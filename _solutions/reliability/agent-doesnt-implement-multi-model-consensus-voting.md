---
title: "Agent Doesn't Implement Multi-Model Consensus Voting"
description: "AI agents rely on a single model call for high-stakes decisions; a single hallucination or reasoning failure goes undetected and is returned to the user as fact."
category: reliability
difficulty: advanced
tags: [consensus, voting, multi-model, reliability, hallucination, ensemble, asyncio]
---

# Agent Doesn't Implement Multi-Model Consensus Voting

## Problem

No single model call is 100% reliable. For factual lookups, classification decisions, code generation, or safety checks, a single model may hallucinate, misclassify, or reason incorrectly. Multi-model consensus queries the same question to multiple models (or the same model multiple times with temperature > 0) and only returns an answer when a majority agree — significantly reducing the probability of undetected errors for high-stakes outputs.

## Solution 1: Majority Vote Across Multiple Model Calls

Query the same prompt to N models; return the answer that appears in a majority of responses.

```python
import asyncio
import re
from anthropic import AsyncAnthropic
from collections import Counter

client = AsyncAnthropic()

MODELS = [
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",  # third vote (same model, different temp)
]

async def single_vote(model: str, prompt: str, temperature: float = 0.0) -> str:
    resp = await client.messages.create(
        model=model,
        max_tokens=64,
        temperature=temperature,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()

async def majority_vote(
    prompt: str,
    models: list[str] | None = None,
    temperatures: list[float] | None = None,
    required_agreement: float = 0.6,  # 60% of votes must agree
) -> tuple[str | None, dict]:
    """
    Query multiple models; return answer if sufficient agreement.
    Returns (answer, metadata) where answer is None if no consensus.
    """
    if models is None:
        models = MODELS
    if temperatures is None:
        temperatures = [0.0] * len(models)

    tasks = [
        single_vote(m, prompt, t)
        for m, t in zip(models, temperatures)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Filter out errors
    answers = [r for r in results if isinstance(r, str)]
    errors = [r for r in results if isinstance(r, Exception)]

    if not answers:
        return None, {"error": "all_models_failed", "errors": [str(e) for e in errors]}

    # Normalize answers for comparison (lowercase, strip whitespace)
    normalized = [a.lower().strip() for a in answers]
    counts = Counter(normalized)
    most_common_answer, vote_count = counts.most_common(1)[0]
    agreement = vote_count / len(answers)

    # Find original-cased version of most common answer
    for original, norm in zip(answers, normalized):
        if norm == most_common_answer:
            winner = original
            break

    metadata = {
        "total_votes": len(answers),
        "agreement": round(agreement, 2),
        "vote_distribution": dict(counts),
        "errors": len(errors),
        "consensus_reached": agreement >= required_agreement,
    }

    if agreement >= required_agreement:
        return winner, metadata
    return None, metadata  # no consensus

# Usage for high-stakes classification
async def classify_with_consensus(text: str) -> dict:
    prompt = f"""Classify the sentiment of this text as exactly one word: positive, negative, or neutral.
Text: {text}
Answer (one word only):"""

    answer, meta = await majority_vote(
        prompt,
        temperatures=[0.0, 0.0, 0.2],  # slight temperature variation on third vote
        required_agreement=0.67,
    )
    return {"classification": answer, "confidence": meta["agreement"], **meta}
```

**When to use**: Classification tasks, factual lookups, safety checks, medical/legal/financial decisions.

---

## Solution 2: Self-Consistency Sampling (Same Model, Multiple Temperatures)

Query the same high-capability model multiple times with temperature > 0 and take the most consistent answer.

```python
import asyncio
import json
from anthropic import AsyncAnthropic
from collections import Counter

client = AsyncAnthropic()

async def self_consistency_vote(
    prompt: str,
    n_samples: int = 5,
    model: str = "claude-sonnet-4-6",
    temperature: float = 0.7,
    extract_fn=None,
) -> tuple[Any, float]:
    """
    Sample the same model N times with temperature>0.
    Returns (most_common_answer, agreement_rate).
    """
    tasks = [
        client.messages.create(
            model=model,
            max_tokens=256,
            temperature=temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        for _ in range(n_samples)
    ]
    responses = await asyncio.gather(*tasks, return_exceptions=True)

    answers = []
    for resp in responses:
        if isinstance(resp, Exception):
            continue
        text = resp.content[0].text.strip()
        if extract_fn:
            extracted = extract_fn(text)
            if extracted is not None:
                answers.append(extracted)
        else:
            answers.append(text)

    if not answers:
        return None, 0.0

    counts = Counter(str(a) for a in answers)
    most_common_str, count = counts.most_common(1)[0]
    agreement = count / len(answers)

    # Return original type if extract_fn was used
    for a in answers:
        if str(a) == most_common_str:
            return a, agreement

    return most_common_str, agreement

def extract_number(text: str) -> float | None:
    """Extract the first number from a response."""
    import re
    match = re.search(r"[-+]?\d*\.?\d+", text)
    return float(match.group()) if match else None

# Usage: math reasoning with self-consistency
async def solve_with_consistency(problem: str) -> dict:
    prompt = f"Solve this math problem step by step. End with 'Answer: <number>'.\n\n{problem}"
    answer, agreement = await self_consistency_vote(
        prompt,
        n_samples=5,
        temperature=0.7,
        extract_fn=extract_number,
    )
    return {
        "answer": answer,
        "agreement": round(agreement, 2),
        "confident": agreement >= 0.6,
    }
```

**When to use**: Complex reasoning tasks where the correct answer exists but the model may reason incorrectly. Self-consistency improves accuracy by 5–15% on math benchmarks.

---

## Solution 3: Weighted Voting — Trust Higher-Capability Models More

Assign weights to each model based on capability; compute a weighted vote rather than simple majority.

```python
import asyncio
from anthropic import AsyncAnthropic
from collections import defaultdict

client = AsyncAnthropic()

MODEL_WEIGHTS = {
    "claude-opus-4-6": 3.0,
    "claude-sonnet-4-6": 2.0,
    "claude-haiku-4-5-20251001": 1.0,
}

async def weighted_vote(
    prompt: str,
    models: list[str] | None = None,
    threshold: float = 0.5,
) -> tuple[str | None, dict]:
    """
    Weighted vote: higher-capability models count for more.
    Returns winner if its weight share exceeds threshold.
    """
    if models is None:
        models = list(MODEL_WEIGHTS.keys())

    async def call(model: str) -> tuple[str, float]:
        resp = await asyncio.wait_for(
            client.messages.create(
                model=model,
                max_tokens=64,
                temperature=0.0,
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=15.0,
        )
        return resp.content[0].text.strip().lower(), MODEL_WEIGHTS.get(model, 1.0)

    results = await asyncio.gather(*[call(m) for m in models], return_exceptions=True)

    # Aggregate weighted votes
    weight_by_answer: dict[str, float] = defaultdict(float)
    total_weight = 0.0
    for r in results:
        if isinstance(r, Exception):
            continue
        answer, weight = r
        weight_by_answer[answer] += weight
        total_weight += weight

    if not weight_by_answer:
        return None, {"error": "all_models_failed"}

    winner = max(weight_by_answer, key=weight_by_answer.get)
    winner_share = weight_by_answer[winner] / total_weight if total_weight > 0 else 0.0

    meta = {
        "winner_weight_share": round(winner_share, 3),
        "total_weight": round(total_weight, 1),
        "weight_distribution": {k: round(v, 2) for k, v in weight_by_answer.items()},
        "consensus": winner_share >= threshold,
    }

    return winner if winner_share >= threshold else None, meta
```

**When to use**: Multi-model ensembles where model quality varies significantly. Opus votes count more than Haiku.

---

## Solution 4: Cascading Consensus — Cheap Models First, Escalate on Disagreement

Run cheap models first; escalate to expensive models only when they disagree.

```python
import asyncio
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

TIERS = [
    # Tier 1: fast + cheap
    ["claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001"],
    # Tier 2: medium (escalate if tier 1 disagrees)
    ["claude-sonnet-4-6"],
    # Tier 3: best (escalate if still no consensus)
    ["claude-opus-4-6"],
]

async def cascading_consensus(prompt: str, required_agreement: float = 1.0) -> dict:
    """
    Start with cheapest models. Escalate only if they disagree.
    Cost-optimized: most queries resolve at tier 1.
    """
    all_answers = []
    tier_used = 0

    for tier_idx, tier_models in enumerate(TIERS):
        tier_used = tier_idx + 1
        tier_tasks = [
            asyncio.wait_for(
                client.messages.create(
                    model=m,
                    max_tokens=64,
                    temperature=0.0,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=10.0,
            )
            for m in tier_models
        ]
        responses = await asyncio.gather(*tier_tasks, return_exceptions=True)

        tier_answers = [
            r.content[0].text.strip().lower()
            for r in responses
            if not isinstance(r, Exception)
        ]
        all_answers.extend(tier_answers)

        if not tier_answers:
            continue

        # Check if all answers so far agree
        unique_answers = set(all_answers)
        if len(unique_answers) == 1:
            return {
                "answer": all_answers[0],
                "tier_resolved": tier_used,
                "total_models": len(all_answers),
                "consensus": True,
            }

        # If at last tier, return most common
        if tier_idx == len(TIERS) - 1:
            from collections import Counter
            winner = Counter(all_answers).most_common(1)[0][0]
            return {
                "answer": winner,
                "tier_resolved": tier_used,
                "total_models": len(all_answers),
                "consensus": False,
                "disagreement": list(unique_answers),
            }

    return {"answer": None, "consensus": False, "error": "no_valid_responses"}

# Expected cost: ~80% of queries resolve at tier 1 (cheap)
```

**When to use**: High-volume agents where most queries are easy but some need expert verification.

---

## Solution 5: Structured Answer Extraction + Cross-Model Verification

Extract structured answers (True/False, numbers, options) for clean comparison across models.

```python
import asyncio
import json
import re
from anthropic import AsyncAnthropic
from typing import TypeVar

client = AsyncAnthropic()
T = TypeVar("T")

STRUCTURED_ANSWER_SYSTEM = """You are a precise answering machine.
Always respond with ONLY a JSON object matching the requested format.
No explanation. No markdown. Pure JSON."""

async def structured_consensus(
    question: str,
    output_schema: dict,
    n_calls: int = 3,
    model: str = "claude-sonnet-4-6",
) -> tuple[dict | None, float]:
    """
    Query model N times, extract structured JSON, find consensus.
    """
    prompt = f"""{question}

Respond with JSON matching this schema:
{json.dumps(output_schema, indent=2)}"""

    tasks = [
        client.messages.create(
            model=model,
            max_tokens=256,
            temperature=0.3,
            system=STRUCTURED_ANSWER_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        for _ in range(n_calls)
    ]
    responses = await asyncio.gather(*tasks, return_exceptions=True)

    parsed_answers = []
    for resp in responses:
        if isinstance(resp, Exception):
            continue
        text = resp.content[0].text.strip()
        try:
            # Strip markdown if present
            if "```" in text:
                text = re.sub(r"```(?:json)?", "", text).strip()
            data = json.loads(text)
            parsed_answers.append(data)
        except json.JSONDecodeError:
            pass

    if not parsed_answers:
        return None, 0.0

    # Find the answer that appears most (serialize for comparison)
    serialized = [json.dumps(a, sort_keys=True) for a in parsed_answers]
    from collections import Counter
    winner_serial, count = Counter(serialized).most_common(1)[0]
    agreement = count / len(parsed_answers)

    return json.loads(winner_serial), agreement

# Usage
async def verify_claim(claim: str) -> dict:
    schema = {
        "verdict": "true | false | uncertain",
        "confidence": "high | medium | low",
        "key_reason": "string (max 20 words)",
    }
    answer, agreement = await structured_consensus(
        f"Is this claim factually accurate? Claim: {claim}",
        schema,
        n_calls=3,
    )
    return {"answer": answer, "inter_model_agreement": round(agreement, 2)}
```

**When to use**: Fact-checking, claim verification, structured classification tasks.

---

## Solution 6: Confidence-Gated Consensus — Vote Only When Model Is Uncertain

Ask each model for a confidence score; use consensus only when confidence is below a threshold.

```python
import asyncio
import json
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

async def confidence_gated_response(
    prompt: str,
    primary_model: str = "claude-sonnet-4-6",
    fallback_models: list[str] | None = None,
    confidence_threshold: float = 0.85,
) -> dict:
    """
    Single model call with self-reported confidence.
    If confidence < threshold, escalate to multi-model vote.
    """
    if fallback_models is None:
        fallback_models = ["claude-opus-4-6", "claude-sonnet-4-6"]

    confidence_prompt = f"""{prompt}

Respond with JSON:
{{"answer": "your answer here", "confidence": 0.0-1.0, "reasoning": "brief reason"}}"""

    resp = await client.messages.create(
        model=primary_model,
        max_tokens=256,
        temperature=0.0,
        system="Respond ONLY with valid JSON. No markdown.",
        messages=[{"role": "user", "content": confidence_prompt}],
    )

    try:
        data = json.loads(resp.content[0].text.strip())
        confidence = float(data.get("confidence", 0.0))
    except (json.JSONDecodeError, ValueError):
        confidence = 0.0
        data = {"answer": resp.content[0].text.strip(), "confidence": 0.0}

    if confidence >= confidence_threshold:
        return {
            "answer": data.get("answer"),
            "confidence": confidence,
            "method": "single_model",
            "model": primary_model,
        }

    # Low confidence: escalate to ensemble vote
    import logging
    logging.getLogger(__name__).info(
        "confidence_gate_triggered",
        extra={"confidence": confidence, "threshold": confidence_threshold},
    )

    tasks = [
        client.messages.create(
            model=m,
            max_tokens=64,
            temperature=0.0,
            messages=[{"role": "user", "content": prompt + "\nAnswer directly:"}],
        )
        for m in fallback_models
    ]
    responses = await asyncio.gather(*tasks, return_exceptions=True)
    answers = [r.content[0].text.strip() for r in responses if not isinstance(r, Exception)]

    from collections import Counter
    if answers:
        winner, count = Counter(a.lower() for a in answers).most_common(1)[0]
        ensemble_confidence = count / len(answers)
        return {
            "answer": winner,
            "confidence": ensemble_confidence,
            "method": "ensemble_vote",
            "primary_confidence": confidence,
        }

    return {"answer": data.get("answer"), "confidence": confidence, "method": "fallback"}
```

**When to use**: Agents where most queries are easy (high confidence = fast/cheap) but hard queries need validation.

---

## Comparison

| Solution | Cost | Latency | Accuracy Gain | Structured Output | Best For |
|---|---|---|---|---|---|
| Majority vote | 3× | Parallel | +10–20% | No | Classification, binary decisions |
| Self-consistency | 5× | Parallel | +5–15% | No | Math, reasoning chains |
| Weighted vote | 3–6× | Parallel | +15–25% | No | Mixed model quality |
| Cascading consensus | ~1.2× avg | Sequential | +10% | No | Cost-optimized high-volume |
| Structured extraction | 3× | Parallel | +15% | Yes | Structured decisions |
| Confidence-gated | ~1.1× avg | Sequential | +10–20% | Yes | Adaptive cost control |

**Rule of thumb**: Use self-consistency (5 samples, temperature=0.7) for reasoning tasks. Use cascading consensus (cheap-first) for high-volume classification. Never use single-call for medical, legal, financial, or safety-critical outputs.
