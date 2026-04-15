---
layout: solution
title: "Agent Overclaims Certainty — No Confidence Scores or Uncertainty Signals"
category: hallucination
description: "The agent states guesses as facts. It says 'The answer is X' when it should say 'I believe X, but verify this.' Users trust wrong answers because the agent never signals uncertainty. Confident-sounding hallucinations pass unnoticed. No confidence score means no way to route low-confidence answers to human review."
tags: [hallucination, confidence, uncertainty, calibration, self-consistency, human-review, verification, reliability]
---

# Agent Overclaims Certainty — No Confidence Scores or Uncertainty Signals

## Symptom
- Agent states wrong answers with the same tone as correct ones
- "The capital of France is Paris" and "The function signature is foo(x, y, z)" — same confidence, one is hallucinated
- Agent never says "I'm not sure" or "you should verify this"
- High-stakes decisions (medical, legal, financial) made on guesses presented as facts
- No way to filter low-confidence outputs for human review
- Users report trusting agent answers that turned out to be wrong
- Agent doubles down when challenged rather than acknowledging uncertainty

## Root Cause
LLMs generate the most probable next token regardless of whether the underlying claim is factual. The model's linguistic confidence (fluent, assertive prose) is not correlated with factual accuracy. Without explicit prompting to self-assess, the model defaults to the most confident-sounding phrasing. The fix is to elicit explicit confidence scores via structured output, use self-consistency sampling to estimate reliability, and route low-confidence answers to human review or tool verification.

## Fix

### Option 1: Structured confidence output — force explicit uncertainty scoring

```python
import anthropic
import json
from typing import Any

client = anthropic.Anthropic()

CONFIDENCE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {
            "type": "string",
            "description": "The direct answer to the question"
        },
        "confidence": {
            "type": "number",
            "description": "Confidence score from 0.0 (pure guess) to 1.0 (certain)",
            "minimum": 0.0,
            "maximum": 1.0
        },
        "confidence_label": {
            "type": "string",
            "enum": ["certain", "high", "medium", "low", "guess"],
            "description": "Human-readable confidence level"
        },
        "reasoning": {
            "type": "string",
            "description": "Why you have this level of confidence"
        },
        "caveats": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Specific things the user should verify"
        },
        "needs_verification": {
            "type": "boolean",
            "description": "True if the user should independently verify this answer"
        }
    },
    "required": ["answer", "confidence", "confidence_label", "reasoning", "needs_verification"]
}

def ask_with_confidence(question: str, context: str = "", model: str = "claude-sonnet-4-6") -> dict:
    """
    Ask a question and get back an answer with explicit confidence scoring.
    Returns a dict with answer, confidence (0-1), and verification flags.
    """
    prompt = question
    if context:
        prompt = f"Context:\n{context}\n\nQuestion: {question}"

    response = client.messages.create(
        model=model,
        max_tokens=1024,
        tools=[{
            "name": "respond_with_confidence",
            "description": "Provide an answer with explicit confidence assessment",
            "input_schema": CONFIDENCE_RESPONSE_SCHEMA
        }],
        tool_choice={"type": "tool", "name": "respond_with_confidence"},
        messages=[{
            "role": "user",
            "content": prompt
        }]
    )

    for block in response.content:
        if block.type == "tool_use":
            result = block.input
            # Normalize confidence_label from score if missing
            if "confidence_label" not in result:
                score = result.get("confidence", 0.5)
                result["confidence_label"] = (
                    "certain" if score >= 0.95 else
                    "high" if score >= 0.80 else
                    "medium" if score >= 0.60 else
                    "low" if score >= 0.40 else
                    "guess"
                )
            return result

    return {"answer": "", "confidence": 0.0, "confidence_label": "guess", "needs_verification": True}

# Usage:
result = ask_with_confidence(
    "What is the default timeout for httpx.AsyncClient in Python?",
    model="claude-sonnet-4-6"
)
print(f"Answer: {result['answer']}")
print(f"Confidence: {result['confidence']:.0%} ({result['confidence_label']})")
print(f"Needs verification: {result['needs_verification']}")
if result.get("caveats"):
    print(f"Caveats: {', '.join(result['caveats'])}")
```

### Option 2: Self-consistency sampling — measure agreement across multiple samples

```python
import asyncio
import anthropic
from collections import Counter
from typing import Any

client = anthropic.AsyncAnthropic()

async def sample_once(question: str, model: str) -> str:
    """Get one sampled response at high temperature."""
    response = await client.messages.create(
        model=model,
        max_tokens=256,
        temperature=1.0,  # High temperature for diverse sampling
        messages=[{
            "role": "user",
            "content": (
                f"{question}\n\n"
                "Answer in one sentence. State only the answer, no explanation."
            )
        }]
    )
    return response.content[0].text.strip()

async def self_consistency_check(
    question: str,
    n_samples: int = 5,
    model: str = "claude-sonnet-4-6"
) -> dict:
    """
    Sample N answers at high temperature. High agreement = high confidence.
    Disagreement across samples = the model is uncertain — flag for review.
    """
    samples = await asyncio.gather(*[sample_once(question, model) for _ in range(n_samples)])

    # Count exact matches (works for factual short answers)
    counts = Counter(samples)
    most_common, most_common_count = counts.most_common(1)[0]
    agreement_rate = most_common_count / n_samples

    # For longer answers, use semantic similarity instead of exact match
    unique_answers = list(counts.keys())

    return {
        "answer": most_common,
        "agreement_rate": agreement_rate,
        "confidence": agreement_rate,  # Agreement = confidence proxy
        "confidence_label": (
            "high" if agreement_rate >= 0.8 else
            "medium" if agreement_rate >= 0.6 else
            "low"
        ),
        "all_samples": samples,
        "unique_answers": unique_answers,
        "needs_verification": agreement_rate < 0.6,
        "n_samples": n_samples
    }

# Usage:
result = await self_consistency_check(
    "What Python version introduced f-strings?",
    n_samples=5
)
print(f"Answer: {result['answer']}")
print(f"Agreement: {result['agreement_rate']:.0%} across {result['n_samples']} samples")
print(f"Confidence: {result['confidence_label']}")
# High agreement (4/5 same answer) → high confidence
# Low agreement (all different) → flag for human review
```

### Option 3: System prompt calibration — instruct the model to self-assess

```python
import anthropic

client = anthropic.Anthropic()

CALIBRATED_UNCERTAINTY_SYSTEM = """## Epistemic Honesty Rules

You must calibrate your confidence explicitly in every factual claim.

**Required signal words:**
- "I'm certain that..." — only for things you know with very high confidence (>95%)
- "I believe..." or "I think..." — for things you're fairly sure about (70-90%)
- "I'm not sure, but..." — for uncertain claims (40-70%)
- "I don't know, but I'd guess..." — for guesses (<40% confidence)
- "I don't know." — when you genuinely don't know

**Rules:**
1. NEVER state a specific version number, date, API signature, or configuration value without a confidence signal unless you are certain.
2. If you are guessing, say so. A labelled guess is more useful than a confident hallucination.
3. After uncertain claims, add: "[verify this]" so the user knows to double-check.
4. For technical facts (library versions, function signatures, SQL syntax), prefer "check the official docs" over guessing.
5. If asked about recent events (after your training cutoff), explicitly state you may be out of date.

**Example of WRONG response:**
"The anthropic library's default timeout is 60 seconds."

**Example of RIGHT response:**
"I believe the anthropic library's default timeout is 60 seconds, but I'm not certain — check the httpx timeout settings in the official Anthropic Python SDK docs [verify this]."
"""

def ask_calibrated(question: str, context: str = "") -> str:
    """Ask a question with uncertainty-calibrated system prompt."""
    messages = [{"role": "user", "content": question}]
    if context:
        messages = [{
            "role": "user",
            "content": f"Context:\n{context}\n\nQuestion: {question}"
        }]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=CALIBRATED_UNCERTAINTY_SYSTEM,
        messages=messages
    )
    return response.content[0].text

# Domain-specific calibration for high-stakes areas:
MEDICAL_UNCERTAINTY_SYSTEM = """## Medical Information Disclaimer

You are providing general health information, NOT medical advice.

Rules:
1. Always prefix medical claims with "Generally speaking..." or "Some research suggests..."
2. Never state a diagnosis as certain.
3. End every medical response with: "This is general information only — consult a healthcare provider for personal medical decisions."
4. For drug dosages, interactions, or contraindications: always say "verify with a pharmacist or prescriber."
5. For symptoms: always include when to seek emergency care.
"""

LEGAL_UNCERTAINTY_SYSTEM = """## Legal Information Disclaimer

You are providing general legal information, NOT legal advice.

Rules:
1. Prefix legal claims with "Generally in many jurisdictions..." or "US law often provides..."
2. Always specify jurisdiction uncertainty: laws vary by state/country.
3. Never state a legal outcome as certain.
4. End every legal response with: "This is general information only — consult a licensed attorney for your specific situation."
"""
```

### Option 4: Confidence-gated routing — auto-escalate low-confidence answers

```python
import anthropic
import logging
from dataclasses import dataclass
from typing import Callable, Awaitable, Any

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

@dataclass
class GatedAnswer:
    answer: str
    confidence: float
    routed_to: str  # "direct", "verified", "human_review"
    verification_note: str = ""

class ConfidenceGatedRouter:
    """
    Routes answers based on confidence score:
    - High confidence → answer directly
    - Medium confidence → verify with tool or second model
    - Low confidence → route to human review queue
    """

    def __init__(
        self,
        high_threshold: float = 0.85,
        low_threshold: float = 0.50,
        verification_fn: Callable[[str, str], str] | None = None,
        human_review_fn: Callable[[str, str, float], None] | None = None
    ):
        self.high_threshold = high_threshold
        self.low_threshold = low_threshold
        self.verification_fn = verification_fn
        self.human_review_fn = human_review_fn

    def _get_answer_with_confidence(self, question: str) -> dict:
        """Get structured answer with confidence from Claude."""
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=[{
                "name": "answer",
                "description": "Provide answer with confidence score",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "answer": {"type": "string"},
                        "confidence": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                            "description": "0=pure guess, 1=certain"
                        },
                        "uncertainty_reason": {"type": "string"}
                    },
                    "required": ["answer", "confidence"]
                }
            }],
            tool_choice={"type": "tool", "name": "answer"},
            messages=[{"role": "user", "content": question}]
        )
        for block in response.content:
            if block.type == "tool_use":
                return block.input
        return {"answer": "Unknown", "confidence": 0.0}

    def route(self, question: str) -> GatedAnswer:
        result = self._get_answer_with_confidence(question)
        answer = result.get("answer", "")
        confidence = result.get("confidence", 0.0)
        uncertainty = result.get("uncertainty_reason", "")

        if confidence >= self.high_threshold:
            logger.info(f"High confidence ({confidence:.0%}): answering directly")
            return GatedAnswer(
                answer=answer,
                confidence=confidence,
                routed_to="direct"
            )

        elif confidence >= self.low_threshold and self.verification_fn:
            logger.info(f"Medium confidence ({confidence:.0%}): verifying with tool")
            verified = self.verification_fn(question, answer)
            return GatedAnswer(
                answer=verified,
                confidence=confidence,
                routed_to="verified",
                verification_note=f"Original answer verified. Uncertainty: {uncertainty}"
            )

        else:
            logger.warning(f"Low confidence ({confidence:.0%}): routing to human review")
            if self.human_review_fn:
                self.human_review_fn(question, answer, confidence)
            return GatedAnswer(
                answer=f"[Uncertain — under review] Best guess: {answer}",
                confidence=confidence,
                routed_to="human_review",
                verification_note=f"Confidence too low ({confidence:.0%}). Reason: {uncertainty}"
            )

# Usage:
def web_search_verify(question: str, proposed_answer: str) -> str:
    """Verify answer with web search (pseudo-code)."""
    # In production: call a search API and verify the claim
    return proposed_answer  # Return verified or corrected answer

def add_to_review_queue(question: str, answer: str, confidence: float):
    logger.warning(f"REVIEW QUEUE: q={question!r} a={answer!r} conf={confidence:.0%}")

router = ConfidenceGatedRouter(
    high_threshold=0.85,
    low_threshold=0.50,
    verification_fn=web_search_verify,
    human_review_fn=add_to_review_queue
)

answer = router.route("What is the exact syntax for asyncio.timeout() in Python 3.11?")
print(f"Answer: {answer.answer}")
print(f"Routed to: {answer.routed_to}")
```

### Option 5: Multi-model consensus — cross-check with a second model

```python
import asyncio
import anthropic
from dataclasses import dataclass

@dataclass
class ConsensusResult:
    answer: str
    agreement: bool
    confidence: float
    model_a_answer: str
    model_b_answer: str
    disagreement_note: str = ""

async def get_answer_from_model(
    question: str,
    model: str,
    client: anthropic.AsyncAnthropic
) -> str:
    response = await client.messages.create(
        model=model,
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                f"{question}\n\n"
                "State your answer as a single definitive claim. "
                "If you're uncertain, say so explicitly."
            )
        }]
    )
    return response.content[0].text.strip()

async def cross_model_consensus(
    question: str,
    model_a: str = "claude-sonnet-4-6",
    model_b: str = "claude-haiku-4-5-20251001"
) -> ConsensusResult:
    """
    Ask two different models the same question.
    Agreement = higher confidence. Disagreement = flag for review.
    """
    client = anthropic.AsyncAnthropic()

    answer_a, answer_b = await asyncio.gather(
        get_answer_from_model(question, model_a, client),
        get_answer_from_model(question, model_b, client)
    )

    # Ask a third call to assess agreement:
    judge_response = await client.messages.create(
        model=model_a,
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                f"Question: {question}\n\n"
                f"Answer A: {answer_a}\n\n"
                f"Answer B: {answer_b}\n\n"
                "Do these answers agree? Reply with JSON: "
                '{"agree": true/false, "synthesis": "the best combined answer", "confidence": 0.0-1.0}'
            )
        }]
    )

    import json
    try:
        judgment = json.loads(judge_response.content[0].text)
        agree = judgment.get("agree", False)
        synthesis = judgment.get("synthesis", answer_a)
        confidence = judgment.get("confidence", 0.5 if agree else 0.2)
    except (json.JSONDecodeError, KeyError):
        agree = False
        synthesis = answer_a
        confidence = 0.3

    return ConsensusResult(
        answer=synthesis,
        agreement=agree,
        confidence=confidence,
        model_a_answer=answer_a,
        model_b_answer=answer_b,
        disagreement_note="" if agree else f"Models disagreed. A: {answer_a!r} B: {answer_b!r}"
    )

# Usage:
result = await cross_model_consensus(
    "What is the maximum context window of claude-sonnet-4-6?"
)
if result.agreement:
    print(f"Consensus answer: {result.answer} (confidence: {result.confidence:.0%})")
else:
    print(f"Models disagreed — verify manually. {result.disagreement_note}")
```

### Option 6: Uncertainty injection in user-facing text — format answers with signals

```python
import anthropic
import re
from enum import Enum

client = anthropic.Anthropic()

class ConfidenceLevel(Enum):
    CERTAIN = "certain"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    GUESS = "guess"

CONFIDENCE_PREFIXES = {
    ConfidenceLevel.CERTAIN: "",
    ConfidenceLevel.HIGH: "I believe ",
    ConfidenceLevel.MEDIUM: "I think, though I'm not fully certain, that ",
    ConfidenceLevel.LOW: "I'm not sure, but I'd guess that ",
    ConfidenceLevel.GUESS: "This is a rough guess: ",
}

CONFIDENCE_SUFFIXES = {
    ConfidenceLevel.CERTAIN: "",
    ConfidenceLevel.HIGH: "",
    ConfidenceLevel.MEDIUM: " (verify this)",
    ConfidenceLevel.LOW: " — please verify this before relying on it",
    ConfidenceLevel.GUESS: " — I have low confidence in this answer, please look it up",
}

def format_answer_with_confidence(answer: str, confidence_level: ConfidenceLevel) -> str:
    """Wrap an answer with appropriate uncertainty language."""
    prefix = CONFIDENCE_PREFIXES[confidence_level]
    suffix = CONFIDENCE_SUFFIXES[confidence_level]
    if prefix and answer[0].isupper():
        answer = answer[0].lower() + answer[1:]
    return f"{prefix}{answer}{suffix}"

def parse_uncertainty_from_response(text: str) -> ConfidenceLevel:
    """Detect expressed uncertainty from model's natural language output."""
    text_lower = text.lower()
    if any(p in text_lower for p in ["i'm certain", "definitely", "i know that", "it is a fact"]):
        return ConfidenceLevel.CERTAIN
    if any(p in text_lower for p in ["i believe", "i think", "most likely", "probably"]):
        return ConfidenceLevel.HIGH
    if any(p in text_lower for p in ["not sure", "i'm not certain", "might be", "may be", "could be"]):
        return ConfidenceLevel.MEDIUM
    if any(p in text_lower for p in ["i'm unsure", "i don't know for sure", "rough guess", "i'd guess"]):
        return ConfidenceLevel.LOW
    if any(p in text_lower for p in ["i don't know", "i have no idea", "speculating", "just a guess"]):
        return ConfidenceLevel.GUESS
    return ConfidenceLevel.HIGH  # Default: high (model is usually assertive)

def ask_and_label_uncertainty(question: str) -> str:
    """
    Ask a question, detect expressed uncertainty, format with signals.
    """
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=(
            "Answer questions directly. "
            "Express your genuine level of certainty using phrases like "
            "'I believe', 'I'm not sure but', 'I think', or 'I don't know' "
            "when appropriate. Don't pretend to know things you don't."
        ),
        messages=[{"role": "user", "content": question}]
    )
    answer = response.content[0].text.strip()
    level = parse_uncertainty_from_response(answer)
    return answer, level

# Usage:
answer_text, level = ask_and_label_uncertainty(
    "What is the exact rate limit for the Anthropic API on the free tier?"
)
print(f"Confidence level detected: {level.value}")
print(f"Answer: {answer_text}")
```

## Confidence Signal Effectiveness

| Technique | Effort | Reliability | Best For |
|---|---|---|---|
| Structured confidence schema (tool_choice) | Medium | High | Single questions, API use |
| Self-consistency sampling (N=5) | High (5× API cost) | Very high | Critical factual claims |
| System prompt calibration | Low | Medium | General use, cheap |
| Confidence-gated routing | High | High | Production, high-stakes domains |
| Multi-model consensus | High (2× cost) | High | Verifying specific claims |
| Uncertainty text injection | Low | Medium | User-facing formatting |

## Expected Token Savings
Confident wrong answer → user asks follow-up for correction → re-explain + correct: ~2,000 tokens overhead per hallucination
Calibrated uncertain answer → user verifies independently → no correction needed: 0 correction overhead
In high-volume QA agents: 15-20% of answers have medium/low confidence — labelling them prevents proportional correction loops

## Environment
- Any agent providing factual information in high-stakes domains (medical, legal, financial, technical API documentation); also valuable in coding assistants where wrong function signatures are common hallucinations; confidence scoring is most important when the cost of a wrong answer exceeds the cost of "I don't know"
- Source: direct experience; overconfident hallucinations are the top trust-erosion factor cited by users after 2-4 weeks of using an AI assistant — one confident wrong answer undoes weeks of accurate responses
