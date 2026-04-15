---
layout: solution
title: "Agent Doesn't Implement Uncertainty Quantification"
category: hallucination
description: "Agents that respond with equal confidence on everything hallucinate without warning — uncertainty quantification makes agents aware of what they don't know and communicate it to users."
tags: [hallucination, uncertainty, confidence, calibration, reliability, self-assessment]
---

# Agent Doesn't Implement Uncertainty Quantification

## Problem

LLMs generate tokens with the same fluent confidence regardless of whether they're stating a verified fact or fabricating a plausible-sounding answer. Without uncertainty quantification, agents present guesses as certainties, users trust wrong answers, and errors cascade silently through multi-step pipelines. Adding uncertainty signals — explicit confidence scores, hedging language calibrated to actual confidence, or hard stops when confidence is too low — dramatically reduces the harm from hallucinations even when they can't be fully eliminated.

## Solutions

### Option 1: Explicit Confidence Score in Structured Output

Ask the model to return a structured response with an explicit `confidence` field alongside the answer. Gate downstream actions on confidence thresholds.

```python
import anthropic
import json
from pydantic import BaseModel, Field
from typing import Literal

client = anthropic.Anthropic()

class ConfidentResponse(BaseModel):
    answer: str
    confidence: float = Field(..., ge=0.0, le=1.0,
                              description="0.0=pure guess, 1.0=certain fact")
    confidence_basis: str = Field(...,
        description="Why this confidence level: 'well-known fact', 'inference', 'estimate', 'uncertain'")
    caveats: list[str] = Field(default_factory=list,
        description="Known limitations or things the user should verify")
    should_verify: bool = Field(
        ..., description="True if user should independently verify this answer")

STRUCTURED_SYSTEM = """You are a helpful assistant that quantifies its own uncertainty.

For every response, return valid JSON matching this schema:
{
  "answer": "<your answer>",
  "confidence": <float 0.0-1.0>,
  "confidence_basis": "<why this confidence level>",
  "caveats": ["<caveat 1>", ...],
  "should_verify": <true|false>
}

Confidence guidelines:
- 0.9-1.0: Well-established fact you are certain about
- 0.7-0.9: Likely correct but may have nuances or recent changes
- 0.5-0.7: Informed estimate or inference — significant uncertainty
- 0.3-0.5: Weak basis — user should definitely verify
- 0.0-0.3: Mostly guessing — strongly recommend verification

Return ONLY the JSON object, no prose."""

def ask_with_confidence(question: str, confidence_threshold: float = 0.6) -> dict:
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        system=STRUCTURED_SYSTEM,
        messages=[{"role": "user", "content": question}],
    )

    try:
        data = json.loads(resp.content[0].text)
        result = ConfidentResponse(**data)
    except Exception as e:
        return {
            "answer": resp.content[0].text,
            "confidence": 0.5,
            "error": f"Parse failed: {e}",
            "should_verify": True,
        }

    # Gate on threshold
    action = "PROCEED" if result.confidence >= confidence_threshold else "VERIFY_FIRST"

    return {
        "answer": result.answer,
        "confidence": result.confidence,
        "basis": result.confidence_basis,
        "caveats": result.caveats,
        "should_verify": result.should_verify,
        "action": action,
    }

questions = [
    "What is the speed of light in vacuum?",
    "Who won the 2024 Nobel Prize in Chemistry?",
    "What is the current population of Mars?",
    "What is the best Python web framework for a startup?",
]

for q in questions:
    result = ask_with_confidence(q)
    conf = result["confidence"]
    bar = "█" * int(conf * 10) + "░" * (10 - int(conf * 10))
    print(f"[{conf:.2f}] {bar} [{result['action']:12}] {q}")
    print(f"         → {result['answer'][:80]}")
    if result.get("caveats"):
        print(f"         ⚠ {result['caveats'][0]}")
# Expected Token Savings: Prevents downstream agents from acting on low-confidence answers
# Environment: Decision-support agents, medical/legal assistants, research agents
```

### Option 2: Multi-Sample Consistency Check

Run the same question multiple times with non-zero temperature. Measure agreement across samples — high variance = low confidence.

```python
import anthropic
import re
from collections import Counter
from difflib import SequenceMatcher

client = anthropic.Anthropic()

def normalize_answer(text: str) -> str:
    """Strip whitespace and lowercase for comparison."""
    return re.sub(r'\s+', ' ', text.strip().lower())

def pairwise_similarity(answers: list[str]) -> float:
    """Average pairwise similarity across all answer pairs."""
    if len(answers) < 2:
        return 1.0
    pairs = [(answers[i], answers[j])
             for i in range(len(answers))
             for j in range(i + 1, len(answers))]
    scores = [
        SequenceMatcher(None, normalize_answer(a), normalize_answer(b)).ratio()
        for a, b in pairs
    ]
    return sum(scores) / len(scores)

def majority_answer(answers: list[str], threshold: float = 0.7) -> str | None:
    """Find the answer that's most similar to the others."""
    if not answers:
        return None
    # Find the answer with highest average similarity to all others
    best_answer = None
    best_score = -1.0
    for candidate in answers:
        others = [a for a in answers if a != candidate]
        if not others:
            return candidate
        avg_sim = sum(
            SequenceMatcher(None, normalize_answer(candidate), normalize_answer(o)).ratio()
            for o in others
        ) / len(others)
        if avg_sim > best_score:
            best_score = avg_sim
            best_answer = candidate
    return best_answer

def sample_consistency_check(
    question: str,
    n_samples: int = 3,
    temperature: float = 0.8,
    confidence_threshold: float = 0.75,
) -> dict:
    """Ask the same question N times; measure consistency as proxy for confidence."""
    answers = []
    for i in range(n_samples):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            temperature=temperature,
            messages=[{
                "role": "user",
                "content": f"Answer concisely in 1-2 sentences: {question}"
            }],
        )
        answers.append(resp.content[0].text.strip())

    consistency = pairwise_similarity(answers)
    best = majority_answer(answers)
    confident = consistency >= confidence_threshold

    return {
        "question": question,
        "answer": best,
        "consistency_score": round(consistency, 3),
        "n_samples": n_samples,
        "all_answers": answers,
        "confident": confident,
        "recommendation": "use answer" if confident else "verify with authoritative source",
    }

test_questions = [
    "What is 15% of 200?",               # Should be highly consistent
    "When was the Eiffel Tower built?",   # Should be consistent
    "What will the S&P 500 be next month?",  # Should be inconsistent
]

for q in test_questions:
    result = sample_consistency_check(q, n_samples=3)
    conf_icon = "✓" if result["confident"] else "⚠"
    print(f"{conf_icon} Consistency={result['consistency_score']:.2f} | {q}")
    print(f"  Answer: {result['answer'][:100]}")
    print(f"  Recommendation: {result['recommendation']}\n")
# Expected Token Savings: 3× more API calls but prevents acting on fabricated facts
# Environment: High-stakes Q&A, medical/legal/financial queries, fact-checking pipelines
```

### Option 3: Calibrated Hedging Language Injection

Map structured confidence levels to natural-language hedging phrases injected into the system prompt. Ensures the model's language matches its actual uncertainty.

```python
import anthropic
from enum import Enum

client = anthropic.Anthropic()

class ConfidenceLevel(Enum):
    CERTAIN = "certain"
    LIKELY = "likely"
    UNCERTAIN = "uncertain"
    GUESSING = "guessing"

HEDGING_INSTRUCTIONS = {
    ConfidenceLevel.CERTAIN: """
When you are certain of a fact (well-established, verifiable):
- State it directly without hedging: "X is Y."
- Example: "Python was created by Guido van Rossum."
""",
    ConfidenceLevel.LIKELY: """
When you are reasonably confident but not certain:
- Use: "Generally...", "Typically...", "In most cases..."
- Add: "though this may have changed" for time-sensitive info
- Example: "The standard approach is X, though best practices evolve."
""",
    ConfidenceLevel.UNCERTAIN: """
When you have limited confidence:
- Always start with: "I'm not certain, but..." or "To my knowledge..."
- End with: "I recommend verifying this with [authoritative source]."
- Example: "I believe the limit is around X, but please check the official docs."
""",
    ConfidenceLevel.GUESSING: """
When you are mostly guessing:
- Be explicit: "I don't have reliable information on this."
- Offer: "I can make an educated guess, but it may be wrong: ..."
- Always recommend: seeking an authoritative source
""",
}

def build_calibrated_system(base_system: str) -> str:
    hedging_section = "\n\n## Uncertainty Communication Rules\n"
    hedging_section += "Calibrate your language to your actual confidence level:\n"
    for level, instructions in HEDGING_INSTRUCTIONS.items():
        hedging_section += f"\n**{level.value.upper()}:**{instructions}"
    hedging_section += """
## Self-Assessment Requirement
Before answering, internally assess:
1. Is this a verifiable fact I know well? → CERTAIN
2. Is this general knowledge that could be outdated? → LIKELY
3. Is this at the edge of my knowledge? → UNCERTAIN
4. Am I speculating? → GUESSING

Then use the corresponding language style above.
"""
    return base_system + hedging_section

BASE_SYSTEM = "You are a knowledgeable assistant."
CALIBRATED_SYSTEM = build_calibrated_system(BASE_SYSTEM)

questions = [
    "What is the boiling point of water at sea level?",
    "What is the best programming language to learn in 2026?",
    "What are the exact tax rates for small businesses in Singapore?",
    "Will quantum computers replace classical computers within 5 years?",
]

for q in questions:
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        system=CALIBRATED_SYSTEM,
        messages=[{"role": "user", "content": q}],
    )
    print(f"Q: {q}")
    print(f"A: {resp.content[0].text.strip()[:200]}\n")
# Expected Token Savings: No extra API calls — confidence expressed in language, not structure
# Environment: Customer-facing chatbots, info-retrieval agents, general assistants
```

### Option 4: Tool-Grounded Confidence Gating

Require the agent to verify claims with a tool before reporting confidence above a threshold. Unverified claims are automatically flagged.

```python
import anthropic
import json
from datetime import datetime

client = anthropic.Anthropic()

# Simulated knowledge base (replace with real search/DB tools)
KNOWLEDGE_BASE = {
    "python_created": "Python was created by Guido van Rossum and first released in 1991.",
    "speed_of_light": "The speed of light in vacuum is 299,792,458 metres per second.",
    "boiling_point_water": "Water boils at 100°C (212°F) at standard atmospheric pressure (sea level).",
}

def verify_fact(claim: str) -> dict:
    """Stub: search knowledge base for claim verification."""
    claim_lower = claim.lower()
    for key, fact in KNOWLEDGE_BASE.items():
        if any(word in claim_lower for word in key.split("_")):
            return {
                "verified": True,
                "source": "internal_knowledge_base",
                "supporting_fact": fact,
                "confidence": 0.95,
            }
    return {
        "verified": False,
        "source": None,
        "supporting_fact": None,
        "confidence": 0.40,  # Unverified = low confidence
    }

TOOLS = [
    {
        "name": "verify_claim",
        "description": "Verify a factual claim against the knowledge base. Always call this before stating facts with high confidence.",
        "input_schema": {
            "type": "object",
            "properties": {
                "claim": {"type": "string", "description": "The specific factual claim to verify"},
                "topic_keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Keywords describing the topic"
                }
            },
            "required": ["claim"]
        }
    }
]

SYSTEM = """You are a fact-grounded assistant. Before stating any factual claim with high confidence:
1. Call verify_claim() to check it against the knowledge base
2. Report the verification result's confidence level in your response
3. If verification fails, say "I cannot verify this — my confidence is low"

Never claim high confidence (>0.7) without a successful verification."""

def grounded_answer(question: str) -> dict:
    messages = [{"role": "user", "content": question}]
    verifications = []

    while True:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        if resp.stop_reason == "end_turn":
            final_answer = next(
                (b.text for b in resp.content if hasattr(b, "text")), ""
            )
            return {
                "answer": final_answer,
                "verifications": verifications,
                "verified_count": sum(1 for v in verifications if v.get("verified")),
                "total_claims": len(verifications),
            }

        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []

            for block in resp.content:
                if block.type == "tool_use" and block.name == "verify_claim":
                    result = verify_fact(block.input.get("claim", ""))
                    verifications.append({
                        "claim": block.input.get("claim"),
                        **result
                    })
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    })

            messages.append({"role": "user", "content": tool_results})

questions = [
    "When was Python created?",
    "What is the exact GDP of the Moon?",
]

for q in questions:
    result = grounded_answer(q)
    print(f"Q: {q}")
    print(f"A: {result['answer'][:200]}")
    print(f"   Verified {result['verified_count']}/{result['total_claims']} claims\n")
# Expected Token Savings: Prevents cascading hallucinations in multi-step pipelines
# Environment: Research agents, fact-checking bots, decision-support systems
```

### Option 5: Confidence Decay Over Time

Facts have a shelf life. Flag claims about time-sensitive topics (prices, regulations, events) as lower-confidence based on knowledge cutoff distance.

```python
import anthropic
import re
from datetime import date

client = anthropic.Anthropic()

# Topics where knowledge decays quickly
TIME_SENSITIVE_PATTERNS = [
    (r"\b(current|latest|now|today|recent|new)\b", 0.4, "time-sensitive keyword"),
    (r"\b(price|cost|rate|fee|salary|wage)\b", 0.5, "financial figures change"),
    (r"\b(law|regulation|policy|compliance|gdpr|hipaa)\b", 0.55, "regulations change"),
    (r"\b(version|release|update|changelog)\b", 0.55, "software versions change"),
    (r"\b(ceo|president|minister|leader|head of)\b", 0.6, "leadership changes"),
    (r"\b(population|census|statistic|survey)\b", 0.65, "statistics are periodically updated"),
]

KNOWLEDGE_CUTOFF = date(2025, 8, 1)  # Claude's training cutoff

def estimate_temporal_confidence(question: str) -> dict:
    """Estimate confidence decay from training cutoff to today."""
    today = date.today()
    days_since_cutoff = (today - KNOWLEDGE_CUTOFF).days
    months_since_cutoff = days_since_cutoff / 30.0

    base_confidence = 0.85
    decay_reason = None
    pattern_matches = []

    for pattern, confidence_cap, reason in TIME_SENSITIVE_PATTERNS:
        if re.search(pattern, question, re.IGNORECASE):
            pattern_matches.append((confidence_cap, reason))

    if pattern_matches:
        # Apply the most restrictive cap
        min_cap, decay_reason = min(pattern_matches, key=lambda x: x[0])
        base_confidence = min(base_confidence, min_cap)

    # Additional decay: 2% per month since cutoff for time-sensitive topics
    if pattern_matches and months_since_cutoff > 0:
        time_decay = min(0.3, months_since_cutoff * 0.02)
        base_confidence = max(0.1, base_confidence - time_decay)

    return {
        "estimated_confidence": round(base_confidence, 2),
        "decay_reason": decay_reason,
        "months_since_cutoff": round(months_since_cutoff, 1),
        "is_time_sensitive": bool(pattern_matches),
    }

def answer_with_temporal_awareness(question: str) -> str:
    temporal = estimate_temporal_confidence(question)
    confidence = temporal["estimated_confidence"]

    if confidence < 0.5:
        confidence_instruction = (
            f"IMPORTANT: This question is time-sensitive ('{temporal['decay_reason']}') "
            f"and my training data is {temporal['months_since_cutoff']:.0f} months old. "
            f"Your confidence must be low (≤{confidence:.1f}). "
            f"Clearly state your knowledge may be outdated and recommend the user verify with current sources."
        )
    elif confidence < 0.7:
        confidence_instruction = (
            f"Note: This topic may have changed since my training cutoff. "
            f"Express moderate confidence and suggest verification for important decisions."
        )
    else:
        confidence_instruction = "Answer normally."

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=250,
        system=f"You are a helpful assistant. {confidence_instruction}",
        messages=[{"role": "user", "content": question}],
    )

    answer = resp.content[0].text
    meta = (
        f"\n[Confidence estimate: {confidence:.0%} | "
        f"Time-sensitive: {temporal['is_time_sensitive']} | "
        f"Knowledge age: {temporal['months_since_cutoff']:.0f} months]"
    )
    return answer + meta

questions = [
    "What is the speed of light?",
    "What is the current price of Bitcoin?",
    "What is the latest version of Python?",
    "Who is the current CEO of OpenAI?",
]

for q in questions:
    print(f"Q: {q}")
    result = answer_with_temporal_awareness(q)
    print(f"A: {result[:300]}\n")
# Expected Token Savings: Routes time-sensitive queries to search tools before answering
# Environment: Financial advisors, news agents, regulatory compliance bots
```

### Option 6: Uncertainty-Aware Multi-Step Pipeline

In multi-step agentic pipelines, propagate confidence scores through each step. Abort or escalate when accumulated uncertainty exceeds a threshold.

```python
import anthropic
import json
from dataclasses import dataclass, field
from typing import Callable

client = anthropic.Anthropic()

@dataclass
class StepResult:
    step_name: str
    output: str
    confidence: float
    warnings: list[str] = field(default_factory=list)
    aborted: bool = False

@dataclass
class PipelineState:
    steps: list[StepResult] = field(default_factory=list)
    accumulated_confidence: float = 1.0  # Product of step confidences
    aborted: bool = False
    abort_reason: str = ""

    def add_step(self, result: StepResult, abort_threshold: float = 0.25):
        self.steps.append(result)
        # Confidence compounds: 0.9 × 0.8 × 0.7 = 0.504
        self.accumulated_confidence *= result.confidence

        if self.accumulated_confidence < abort_threshold or result.aborted:
            self.aborted = True
            self.abort_reason = (
                f"Accumulated confidence {self.accumulated_confidence:.2f} "
                f"fell below threshold {abort_threshold:.2f} at step '{result.step_name}'"
            )

CONFIDENCE_EXTRACTION_PROMPT = """After answering, append on a new line:
CONFIDENCE: <float 0.0-1.0> | WARNINGS: <comma-separated issues or 'none'>"""

def pipeline_step(
    step_name: str,
    instruction: str,
    input_text: str,
    min_confidence: float = 0.5,
) -> StepResult:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": f"{instruction}\n\nInput: {input_text}\n\n{CONFIDENCE_EXTRACTION_PROMPT}"
        }],
    )

    raw = resp.content[0].text.strip()
    confidence = min_confidence
    warnings = []

    # Extract confidence from last line
    lines = raw.splitlines()
    for line in reversed(lines):
        if line.startswith("CONFIDENCE:"):
            parts = line.split("|")
            try:
                confidence = float(parts[0].split(":")[1].strip())
            except (ValueError, IndexError):
                pass
            if len(parts) > 1:
                warn_text = parts[1].split(":")[1].strip()
                if warn_text.lower() != "none":
                    warnings = [w.strip() for w in warn_text.split(",")]
            output = "\n".join(l for l in lines if not l.startswith("CONFIDENCE:")).strip()
            break
    else:
        output = raw

    return StepResult(
        step_name=step_name,
        output=output,
        confidence=confidence,
        warnings=warnings,
        aborted=confidence < min_confidence,
    )

def run_research_pipeline(topic: str, abort_threshold: float = 0.20) -> dict:
    state = PipelineState()

    # Step 1: Generate research questions
    step1 = pipeline_step(
        "generate_questions",
        "Generate 3 specific research questions about this topic.",
        topic,
        min_confidence=0.5,
    )
    state.add_step(step1, abort_threshold)

    if state.aborted:
        return {"aborted": True, "reason": state.abort_reason, "steps": len(state.steps)}

    # Step 2: Answer questions
    step2 = pipeline_step(
        "answer_questions",
        "Answer these research questions with specific facts. Be honest about uncertainty.",
        step1.output,
        min_confidence=0.4,
    )
    state.add_step(step2, abort_threshold)

    if state.aborted:
        return {"aborted": True, "reason": state.abort_reason, "steps": len(state.steps)}

    # Step 3: Synthesize
    step3 = pipeline_step(
        "synthesize",
        "Synthesize the research into a 3-sentence summary.",
        step2.output,
        min_confidence=0.5,
    )
    state.add_step(step3, abort_threshold)

    return {
        "aborted": state.aborted,
        "final_output": step3.output,
        "accumulated_confidence": round(state.accumulated_confidence, 3),
        "step_confidences": [
            {"step": s.step_name, "confidence": s.confidence, "warnings": s.warnings}
            for s in state.steps
        ],
        "recommendation": "use result" if state.accumulated_confidence > 0.4 else "verify before use",
    }

result = run_research_pipeline("Python async/await best practices")
print(f"Pipeline completed: accumulated_confidence={result['accumulated_confidence']}")
print(f"Recommendation: {result['recommendation']}")
for step in result["step_confidences"]:
    print(f"  [{step['confidence']:.2f}] {step['step']}: {step['warnings'] or 'no warnings'}")
if not result.get("aborted"):
    print(f"\nOutput: {result.get('final_output', '')[:200]}")
# Expected Token Savings: Aborts expensive downstream steps when confidence collapses early
# Environment: Multi-step research agents, document generation pipelines, automated reports
```

## Comparison Table

| Option | Confidence Source | Extra API Calls | Accuracy | Best For |
|--------|------------------|----------------|----------|----------|
| 1: Structured Output | Self-reported JSON | None | Medium | Decision-support, structured pipelines |
| 2: Multi-Sample Consistency | Empirical variance | 2-4× more | High | High-stakes Q&A, fact verification |
| 3: Calibrated Hedging Language | Instructed language style | None | Medium | User-facing chatbots, general assistants |
| 4: Tool-Grounded Gating | External verification | Tool call overhead | Very High | Research agents, fact-checking systems |
| 5: Temporal Decay | Training cutoff distance | None | High for time-sensitive | Financial, regulatory, news agents |
| 6: Pipeline Uncertainty Propagation | Compounding step confidence | Minimal | High | Multi-step agentic pipelines, reports |
