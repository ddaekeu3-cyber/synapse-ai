---
layout: solution
title: "Agent Doesn't Implement Confidence Calibration and Abstention"
category: hallucination
description: "Agents that answer every question with equal confidence hallucinate most on the questions they should refuse. Calibration teaches agents to recognize uncertainty and abstain rather than confabulate."
tags: [confidence, calibration, abstention, uncertainty, hallucination, reliability]
---

# Agent Doesn't Implement Confidence Calibration and Abstention

## The Problem

A well-calibrated agent says "I don't know" when it genuinely doesn't know — rather than fabricating a plausible-sounding answer. Most agents are overconfident: they produce fluent, confident text even when the underlying knowledge is absent or unreliable. This is especially dangerous in high-stakes domains (medical, legal, financial) where a confident wrong answer is worse than a clear "I'm not certain."

Calibration is the gap between stated confidence and actual accuracy. A perfectly calibrated agent that says "80% confident" is right 80% of the time.

---

## Option 1: Explicit Confidence Scoring with Abstention Threshold

Ask the model to score its own confidence; abstain below a threshold.

```python
import anthropic
import json
import re

client = anthropic.Anthropic()

CONFIDENCE_SYSTEM = """You are a precise assistant. For every answer, include a confidence assessment.

Format your responses as:
ANSWER: [your answer]
CONFIDENCE: [0-100]
REASONING: [why you're this confident]

Rules:
- 90-100: You are certain. Verifiable facts you know well.
- 70-89: High confidence. Likely correct but verify for critical use.
- 50-69: Moderate confidence. Significant uncertainty.
- 30-49: Low confidence. You may be wrong.
- 0-29: Very uncertain. Do not rely on this answer.

If CONFIDENCE < 40, begin ANSWER with "I'm not certain, but:" or "I don't have reliable information on this."
"""

def parse_confidence_response(text: str) -> dict:
    """Extract answer, confidence, and reasoning from structured response."""
    answer_match = re.search(r'ANSWER:\s*(.*?)(?:CONFIDENCE:|$)', text, re.DOTALL)
    conf_match = re.search(r'CONFIDENCE:\s*(\d+)', text)
    reason_match = re.search(r'REASONING:\s*(.*?)$', text, re.DOTALL)

    return {
        "answer": answer_match.group(1).strip() if answer_match else text,
        "confidence": int(conf_match.group(1)) if conf_match else 50,
        "reasoning": reason_match.group(1).strip() if reason_match else ""
    }

def calibrated_answer(
    question: str,
    abstention_threshold: int = 40,
    model: str = "claude-sonnet-4-6"
) -> dict:
    """Get an answer with calibrated confidence; abstain if below threshold."""
    response = client.messages.create(
        model=model,
        max_tokens=512,
        system=CONFIDENCE_SYSTEM,
        messages=[{"role": "user", "content": question}]
    )

    parsed = parse_confidence_response(response.content[0].text)
    confidence = parsed["confidence"]
    should_abstain = confidence < abstention_threshold

    return {
        "question": question,
        "answer": parsed["answer"] if not should_abstain else "ABSTAINED",
        "confidence": confidence,
        "reasoning": parsed["reasoning"],
        "abstained": should_abstain,
        "abstain_reason": f"Confidence {confidence}% below threshold {abstention_threshold}%" if should_abstain else None
    }

# Usage: mix of easy, hard, and trick questions
questions = [
    "What is the capital of France?",                          # Should be high confidence
    "Who won the 1987 Grammy for Best New Artist?",            # May be uncertain
    "What will the S&P 500 close at tomorrow?",               # Should abstain
    "What is the boiling point of water at sea level?",       # High confidence
    "What was the exact population of Tokyo on March 3, 2019?",  # Should abstain
]

print("Calibrated answers:")
for q in questions:
    result = calibrated_answer(q, abstention_threshold=45)
    status = "[ABSTAINED]" if result["abstained"] else f"[CONF: {result['confidence']}%]"
    print(f"\nQ: {q}")
    print(f"  {status} {result['answer'][:120]}")
    if result["abstained"]:
        print(f"  Reason: {result['abstain_reason']}")

# Expected Token Savings: Sonnet calibration adds ~50 tokens overhead; prevents costly hallucinations in downstream use
# Environment: medical Q&A, legal research, financial analysis, any high-stakes factual domain
```

---

## Option 2: Multi-Sample Consistency as Confidence Proxy

Generate N responses at higher temperature and use agreement rate as confidence estimate.

```python
import anthropic
from collections import Counter
import re

client = anthropic.Anthropic()

def extract_key_claim(response: str, question: str) -> str:
    """Extract the core factual claim from a response for comparison."""
    # Use Haiku to extract the essential claim
    extract_resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        messages=[{
            "role": "user",
            "content": f"Question: {question}\nResponse: {response}\n\nExtract ONLY the key factual claim as 1-5 words. No explanation."
        }]
    )
    return extract_resp.content[0].text.strip().lower()

def sample_based_confidence(
    question: str,
    n_samples: int = 5,
    temperature_samples: float = 0.8,
    agreement_abstention_threshold: float = 0.4,
    model: str = "claude-haiku-4-5-20251001"
) -> dict:
    """
    Generate N responses; use agreement rate as confidence.
    High agreement → high confidence. Low agreement → abstain.
    """
    responses = []
    for _ in range(n_samples):
        resp = client.messages.create(
            model=model,
            max_tokens=200,
            messages=[{"role": "user", "content": question}]
        )
        responses.append(resp.content[0].text)

    # Extract key claims and compute agreement
    claims = [extract_key_claim(r, question) for r in responses]
    claim_counts = Counter(claims)
    most_common_claim, most_common_count = claim_counts.most_common(1)[0]
    agreement_rate = most_common_count / n_samples

    # Find the full response matching the most common claim
    best_response = next(
        (r for r, c in zip(responses, claims) if c == most_common_claim),
        responses[0]
    )

    should_abstain = agreement_rate < agreement_abstention_threshold

    return {
        "question": question,
        "samples": n_samples,
        "agreement_rate": agreement_rate,
        "confidence_pct": int(agreement_rate * 100),
        "most_common_claim": most_common_claim,
        "all_claims": dict(claim_counts),
        "best_response": best_response if not should_abstain else "ABSTAINED — high disagreement across samples",
        "abstained": should_abstain
    }

# Usage
questions = [
    "What is 2 + 2?",
    "Who invented the telephone?",
    "What will Bitcoin's price be in 2026?",
    "What is the chemical formula for water?",
    "Who will win the next US presidential election?",
]

print("Consistency-based confidence calibration:")
for q in questions:
    result = sample_based_confidence(q, n_samples=3)
    print(f"\nQ: {q}")
    print(f"  Agreement: {result['agreement_rate']:.0%} ({result['confidence_pct']}% confidence)")
    print(f"  Claims: {result['all_claims']}")
    if result["abstained"]:
        print(f"  → ABSTAINED (high disagreement)")
    else:
        print(f"  → {result['best_response'][:100]}")

# Expected Token Savings: 3 Haiku samples cheaper than 1 Sonnet hallucination correction downstream
# Environment: fact-checking pipelines, research assistants, anywhere answer consistency is measurable
```

---

## Option 3: Domain-Specific Knowledge Boundary Detection

Maintain a registry of knowledge domains; check whether the question falls within known reliable boundaries before answering.

```python
import anthropic
import json

client = anthropic.Anthropic()

# Knowledge domain registry with confidence adjustments
DOMAIN_REGISTRY = {
    "mathematics": {
        "description": "Pure math: arithmetic, algebra, calculus, proofs",
        "base_confidence": 0.95,
        "abstain_if_involves": ["very large calculations", "open problems"]
    },
    "well_known_history": {
        "description": "Major historical events, public figures, dates before 2024",
        "base_confidence": 0.85,
        "abstain_if_involves": ["exact statistics", "obscure events"]
    },
    "established_science": {
        "description": "Physics, chemistry, biology facts from textbooks",
        "base_confidence": 0.90,
        "abstain_if_involves": ["cutting-edge research", "contested findings"]
    },
    "current_events": {
        "description": "News, market prices, sports scores, current leaders",
        "base_confidence": 0.20,
        "abstain_if_involves": ["any current events"]
    },
    "predictions": {
        "description": "Future events, forecasts, stock prices",
        "base_confidence": 0.05,
        "abstain_if_involves": ["future", "will", "predict"]
    },
    "personal_information": {
        "description": "Specific people's private information",
        "base_confidence": 0.10,
        "abstain_if_involves": ["private info", "personal details"]
    },
    "code_syntax": {
        "description": "Programming language syntax, standard library APIs",
        "base_confidence": 0.88,
        "abstain_if_involves": ["third-party library specifics", "version-specific APIs"]
    }
}

def classify_question_domain(question: str) -> dict:
    """Classify a question into knowledge domains and estimate confidence."""
    domain_list = json.dumps(
        {k: v["description"] for k, v in DOMAIN_REGISTRY.items()}, indent=2
    )

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"""Classify this question into knowledge domains.

Question: {question}

Available domains:
{domain_list}

Reply with JSON:
{{
  "primary_domain": "domain_name",
  "secondary_domains": ["..."],
  "requires_current_info": true/false,
  "involves_prediction": true/false,
  "confidence_modifiers": ["list any factors that reduce confidence"]
}}"""
        }]
    )
    try:
        return json.loads(resp.content[0].text.strip())
    except json.JSONDecodeError:
        return {"primary_domain": "unknown", "confidence_modifiers": []}

def boundary_aware_answer(
    question: str,
    abstention_threshold: float = 0.5,
    model: str = "claude-sonnet-4-6"
) -> dict:
    """Answer question with domain-aware confidence calibration."""
    classification = classify_question_domain(question)
    primary = classification.get("primary_domain", "unknown")

    domain_info = DOMAIN_REGISTRY.get(primary, {"base_confidence": 0.5, "abstain_if_involves": []})
    base_confidence = domain_info["base_confidence"]

    # Apply modifiers
    modifiers = classification.get("confidence_modifiers", [])
    if classification.get("requires_current_info"):
        base_confidence *= 0.3
        modifiers.append("requires current information")
    if classification.get("involves_prediction"):
        base_confidence *= 0.2
        modifiers.append("involves future prediction")
    if len(modifiers) > 2:
        base_confidence *= 0.8

    should_abstain = base_confidence < abstention_threshold

    if should_abstain:
        answer = (f"I'm not reliably positioned to answer this. "
                  f"Domain: {primary} (confidence: {base_confidence:.0%}). "
                  f"Concerns: {', '.join(modifiers[:3])}.")
    else:
        resp = client.messages.create(
            model=model, max_tokens=512,
            messages=[{"role": "user", "content": question}]
        )
        answer = resp.content[0].text

    return {
        "question": question,
        "primary_domain": primary,
        "estimated_confidence": base_confidence,
        "confidence_modifiers": modifiers,
        "abstained": should_abstain,
        "answer": answer
    }

# Usage
questions = [
    "What is the Pythagorean theorem?",
    "What is the current price of Apple stock?",
    "Who was the 16th President of the United States?",
    "What will the weather be like next Tuesday?",
    "How do you write a for loop in Python?",
    "What are the exact sales figures for Tesla in Q3 2025?",
]

print("Domain-boundary calibrated answers:")
for q in questions:
    result = boundary_aware_answer(q)
    conf_str = f"{result['estimated_confidence']:.0%}"
    status = "ABSTAINED" if result["abstained"] else f"CONF:{conf_str}"
    print(f"\nQ: {q[:70]}")
    print(f"  Domain: {result['primary_domain']} | {status}")
    if result["confidence_modifiers"]:
        print(f"  Modifiers: {result['confidence_modifiers']}")
    print(f"  Answer: {result['answer'][:120]}")

# Expected Token Savings: Haiku domain classifier (~80 tokens) prevents Sonnet hallucination on uncertain domains
# Environment: research assistants, educational tools, expert advisory agents
```

---

## Option 4: Retrieval-Gated Answering

Only answer factual questions when retrieved evidence supports the claim; otherwise abstain.

```python
import anthropic
import json

client = anthropic.Anthropic()

# Simulated knowledge base (replace with real vector search)
KNOWLEDGE_BASE = {
    "python_history": "Python was created by Guido van Rossum and first released in 1991.",
    "water_formula": "Water has the chemical formula H2O, consisting of two hydrogen atoms and one oxygen atom.",
    "speed_of_light": "The speed of light in a vacuum is approximately 299,792,458 meters per second (≈3×10^8 m/s).",
    "paris_capital": "Paris is the capital and largest city of France.",
    "sorting_complexity": "Quicksort has average O(n log n) time complexity and O(log n) space complexity.",
}

def retrieve_evidence(question: str, knowledge_base: dict) -> list[dict]:
    """Find relevant knowledge base entries for a question."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": f"""Which of these knowledge base entries are relevant to answering this question?

Question: {question}

Knowledge base entries:
{json.dumps(list(knowledge_base.keys()), indent=2)}

Reply with JSON: {{"relevant_keys": ["key1", "key2"], "relevance_scores": {{"key1": 0.9}}}}"""
        }]
    )
    try:
        result = json.loads(resp.content[0].text.strip())
        relevant_keys = result.get("relevant_keys", [])
        scores = result.get("relevance_scores", {})
        return [
            {"key": k, "content": knowledge_base[k], "score": scores.get(k, 0.5)}
            for k in relevant_keys if k in knowledge_base
        ]
    except json.JSONDecodeError:
        return []

def assess_evidence_sufficiency(question: str, evidence: list[dict]) -> dict:
    """Check if retrieved evidence is sufficient to answer the question."""
    if not evidence:
        return {"sufficient": False, "confidence": 0.0, "reason": "No relevant evidence found"}

    evidence_text = "\n".join(f"- {e['content']}" for e in evidence)

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{
            "role": "user",
            "content": f"""Is this evidence sufficient to accurately answer the question?

Question: {question}
Evidence:
{evidence_text}

Reply with JSON:
{{"sufficient": true/false, "confidence": 0.0-1.0, "reason": "brief explanation"}}"""
        }]
    )
    try:
        return json.loads(resp.content[0].text.strip())
    except json.JSONDecodeError:
        return {"sufficient": False, "confidence": 0.0, "reason": "Assessment failed"}

def retrieval_gated_answer(
    question: str,
    knowledge_base: dict,
    sufficiency_threshold: float = 0.7,
    model: str = "claude-sonnet-4-6"
) -> dict:
    """Answer only when retrieved evidence is sufficient; otherwise abstain."""
    evidence = retrieve_evidence(question, knowledge_base)
    assessment = assess_evidence_sufficiency(question, evidence)

    if not assessment["sufficient"] or assessment["confidence"] < sufficiency_threshold:
        return {
            "question": question,
            "answer": f"I don't have reliable information to answer this accurately. "
                      f"({assessment['reason']})",
            "evidence_found": len(evidence),
            "evidence_sufficient": False,
            "confidence": assessment["confidence"],
            "abstained": True,
            "sources": []
        }

    # Build grounded answer from evidence
    evidence_context = "\n".join(f"- {e['content']}" for e in evidence)
    resp = client.messages.create(
        model=model, max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"""Answer this question using ONLY the provided evidence.
If the evidence doesn't fully answer the question, say so.

Question: {question}
Evidence:
{evidence_context}"""
        }]
    )

    return {
        "question": question,
        "answer": resp.content[0].text,
        "evidence_found": len(evidence),
        "evidence_sufficient": True,
        "confidence": assessment["confidence"],
        "abstained": False,
        "sources": [e["key"] for e in evidence]
    }

# Usage
questions = [
    "What is the chemical formula for water?",
    "Who created Python?",
    "What is the stock price of NVDA today?",
    "What is the speed of light?",
    "Who won the World Cup in 2026?",
]

print("Retrieval-gated answers:")
for q in questions:
    result = retrieval_gated_answer(q, KNOWLEDGE_BASE)
    status = "ABSTAINED" if result["abstained"] else f"CONF:{result['confidence']:.0%}"
    print(f"\nQ: {q}")
    print(f"  {status} | Evidence: {result['evidence_found']} entries | Sources: {result['sources']}")
    print(f"  → {result['answer'][:120]}")

# Expected Token Savings: Haiku retrieval assessment prevents Sonnet from hallucinating on unsupported questions
# Environment: RAG systems, knowledge base Q&A, document-grounded assistants
```

---

## Option 5: Calibration Self-Audit Loop

After answering, run a second pass where the model audits its own answer for overconfidence.

```python
import anthropic
import json

client = anthropic.Anthropic()

def generate_initial_answer(question: str, model: str = "claude-sonnet-4-6") -> str:
    """Generate initial answer without calibration."""
    resp = client.messages.create(
        model=model, max_tokens=512,
        messages=[{"role": "user", "content": question}]
    )
    return resp.content[0].text

def self_audit_for_overconfidence(
    question: str,
    answer: str
) -> dict:
    """Have the model audit its own answer for overconfidence."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": f"""Audit this answer for overconfidence or potential inaccuracies.

Question: {question}
Answer: {answer}

Identify:
1. Any claims stated with more certainty than warranted
2. Any claims that may be outdated or unverifiable
3. Any claims that are fabricated-sounding

Reply with JSON:
{{
  "overconfident_claims": ["claim 1", "claim 2"],
  "potentially_outdated": ["claim"],
  "unverifiable_claims": ["claim"],
  "overall_risk": "low/medium/high",
  "suggested_caveats": ["add caveat X", "say 'I believe' before Y"]
}}"""
        }]
    )
    try:
        return json.loads(resp.content[0].text.strip())
    except json.JSONDecodeError:
        return {"overall_risk": "medium", "suggested_caveats": [], "overconfident_claims": []}

def add_calibration_caveats(
    question: str,
    answer: str,
    audit: dict
) -> str:
    """Rewrite the answer with appropriate caveats based on audit."""
    if audit.get("overall_risk") == "low" and not audit.get("overconfident_claims"):
        return answer

    caveats = audit.get("suggested_caveats", [])
    overconfident = audit.get("overconfident_claims", [])
    outdated = audit.get("potentially_outdated", [])

    caveat_text = ""
    if overconfident or outdated:
        items = [f"- {c}" for c in (overconfident + outdated)[:3]]
        caveat_text = "\n\n[Note: The following claims carry uncertainty: " + "; ".join(items[:2]) + "]"

    return answer + caveat_text

def calibrated_answer_with_audit(
    question: str,
    high_risk_abstain: bool = True
) -> dict:
    """Generate answer, audit for overconfidence, add caveats or abstain."""
    initial = generate_initial_answer(question)
    audit = self_audit_for_overconfidence(question, initial)

    risk = audit.get("overall_risk", "medium")

    if risk == "high" and high_risk_abstain:
        return {
            "question": question,
            "answer": "I cannot provide a reliable answer to this question. "
                      "My initial response contained high-risk overconfident claims: "
                      + ", ".join(audit.get("overconfident_claims", [])[:2]),
            "initial_answer": initial,
            "audit": audit,
            "abstained": True
        }

    calibrated = add_calibration_caveats(question, initial, audit)

    return {
        "question": question,
        "answer": calibrated,
        "initial_answer": initial,
        "audit": audit,
        "abstained": False,
        "risk_level": risk,
        "caveats_added": calibrated != initial
    }

# Usage
questions = [
    "What is photosynthesis?",
    "What are the exact 2025 tax brackets in the United States?",
    "How does TCP/IP work?",
    "Who are the top 5 AI companies by revenue in 2025?",
]

print("Self-audited calibrated answers:")
for q in questions:
    result = calibrated_answer_with_audit(q)
    status = "ABSTAINED" if result["abstained"] else f"RISK:{result.get('risk_level', '?')}"
    print(f"\nQ: {q}")
    print(f"  {status} | Caveats added: {result.get('caveats_added', False)}")
    if result["audit"].get("overconfident_claims"):
        print(f"  Overconfident claims flagged: {result['audit']['overconfident_claims'][:2]}")
    print(f"  Answer: {result['answer'][:150]}")

# Expected Token Savings: Haiku auditor adds ~100 tokens to catch overconfidence; prevents costly errors downstream
# Environment: financial advisory, medical information, legal guidance, educational Q&A
```

---

## Option 6: Calibration Measurement and Tracking

Measure actual calibration quality over time by comparing stated confidence to observed accuracy.

```python
import anthropic
import json
import sqlite3
import re
from contextlib import contextmanager
from datetime import datetime

client = anthropic.Anthropic()

CALIB_DB = "calibration_history.db"

@contextmanager
def get_db():
    conn = sqlite3.connect(CALIB_DB)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_calibration_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS calibration_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT,
                stated_confidence INTEGER,
                answer TEXT,
                correct INTEGER,
                ground_truth TEXT,
                timestamp TEXT
            )
        """)

def get_calibration_curve() -> list[dict]:
    """Compute calibration: for each confidence bucket, what % were actually correct?"""
    with get_db() as db:
        rows = db.execute("""
            SELECT
                (stated_confidence / 10) * 10 as confidence_bucket,
                COUNT(*) as total,
                SUM(correct) as correct_count,
                CAST(SUM(correct) AS REAL) / COUNT(*) as accuracy
            FROM calibration_records
            WHERE correct IS NOT NULL
            GROUP BY confidence_bucket
            ORDER BY confidence_bucket
        """).fetchall()
        return [dict(r) for r in rows]

def record_prediction(question: str, stated_confidence: int, answer: str):
    """Record a prediction for later verification."""
    with get_db() as db:
        db.execute("""
            INSERT INTO calibration_records
            (question, stated_confidence, answer, timestamp)
            VALUES (?, ?, ?, ?)
        """, (question, stated_confidence, answer, datetime.utcnow().isoformat()))

def verify_answer(question: str, answer: str, ground_truth: str, correct: bool):
    """Record verification result for a previously answered question."""
    with get_db() as db:
        db.execute("""
            UPDATE calibration_records
            SET correct = ?, ground_truth = ?
            WHERE question = ? AND correct IS NULL
            ORDER BY id DESC LIMIT 1
        """, (int(correct), ground_truth, question))

def measure_calibration_error() -> dict:
    """Compute Expected Calibration Error (ECE)."""
    curve = get_calibration_curve()
    if not curve:
        return {"ece": None, "message": "No verified predictions yet"}

    total = sum(b["total"] for b in curve)
    ece = sum(
        (b["total"] / total) * abs(b["confidence_bucket"] / 100 - b["accuracy"])
        for b in curve
        if b["accuracy"] is not None
    )

    return {
        "expected_calibration_error": round(ece, 3),
        "interpretation": "0=perfect calibration, 0.1=poor, 0.2+=very poor",
        "calibration_curve": curve,
        "total_predictions": total
    }

def ask_with_confidence_tracking(question: str, model: str = "claude-sonnet-4-6") -> dict:
    """Ask question with confidence scoring; record for calibration measurement."""
    resp = client.messages.create(
        model=model, max_tokens=512,
        system="""Answer questions and include your confidence.
Format: ANSWER: [text] CONFIDENCE: [0-100]""",
        messages=[{"role": "user", "content": question}]
    )
    text = resp.content[0].text

    answer_match = re.search(r'ANSWER:\s*(.*?)(?:CONFIDENCE:|$)', text, re.DOTALL)
    conf_match = re.search(r'CONFIDENCE:\s*(\d+)', text)

    answer = answer_match.group(1).strip() if answer_match else text
    confidence = int(conf_match.group(1)) if conf_match else 50

    record_prediction(question, confidence, answer)

    return {"question": question, "answer": answer, "confidence": confidence}

# Demo: simulate predictions and verifications
init_calibration_db()

# Simulate some predictions with known answers
test_data = [
    ("What is 7 * 8?", "56", True),
    ("What is the capital of Australia?", "Canberra", True),
    ("What is the capital of Australia?", "Sydney", False),  # Wrong prediction
    ("Who invented the internet?", "Tim Berners-Lee invented WWW", True),
    ("What will Bitcoin be worth next year?", "Unknown", False),  # Speculative
]

print("Simulating calibration measurements:")
for question, ground_truth, correct in test_data:
    result = ask_with_confidence_tracking(question)
    verify_answer(question, result["answer"], ground_truth, correct)
    print(f"  Q: {question[:50]} | Confidence: {result['confidence']}% | Correct: {correct}")

# Measure calibration
calib = measure_calibration_error()
print(f"\nCalibration Report:")
if calib.get("ece") is not None:
    print(f"  Expected Calibration Error (ECE): {calib['expected_calibration_error']}")
    print(f"  {calib['interpretation']}")
    print(f"  Calibration curve:")
    for bucket in calib.get("calibration_curve", []):
        bar = "█" * int((bucket["accuracy"] or 0) * 20)
        print(f"    {bucket['confidence_bucket']:3d}% stated → {(bucket['accuracy'] or 0):.0%} actual ({bucket['total']} samples) {bar}")
else:
    print(f"  {calib.get('message')}")

# Expected Token Savings: Tracking calibration over time identifies systematic overconfidence; fix once, save forever
# Environment: production agents, model evaluation, trust calibration for autonomous systems
```

---

## Comparison

| Option | Detection Method | Abstention Trigger | Extra API Calls | Best For |
|--------|-----------------|-------------------|-----------------|----------|
| 1. Explicit Scoring | Self-scored 0-100 | Below threshold | 0 | General agents, quick setup |
| 2. Multi-Sample | Agreement across N samples | Low agreement | N-1 | High-stakes facts, no ground truth |
| 3. Domain Boundaries | Domain classifier | Unknown domain | 1 (Haiku) | Domain-specific knowledge bases |
| 4. Retrieval-Gated | Evidence sufficiency | Insufficient evidence | 2 (Haiku) | RAG systems, grounded QA |
| 5. Self-Audit | Post-answer overconfidence audit | High risk claims | 1 (Haiku) | Expert advice, formal outputs |
| 6. Calibration Tracking | ECE measurement | Systematic bias correction | Historical | Model evaluation, trust tuning |

**Recommended defaults:**
- **Immediate production use** → Option 1 (explicit scoring) + Option 5 (self-audit)
- **RAG/knowledge base** → Option 4 (retrieval-gated)
- **High-stakes domains** → Option 2 (multi-sample) for critical questions
- **Long-term quality** → Option 6 (calibration tracking) to measure and improve over time
