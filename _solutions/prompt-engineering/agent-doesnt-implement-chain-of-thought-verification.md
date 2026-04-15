---
layout: solution
title: "Agent Doesn't Implement Chain-of-Thought Verification"
category: prompt-engineering
description: "When agents generate multi-step reasoning without checking internal consistency, they produce confident but flawed conclusions. These patterns verify CoT steps before acting on them."
tags: [chain-of-thought, reasoning, verification, consistency, self-critique, prompt-engineering]
---

# Agent Doesn't Implement Chain-of-Thought Verification

## The Problem

Agents that use chain-of-thought prompting generate intermediate reasoning steps, but rarely verify whether those steps are internally consistent, logically sound, or factually grounded. A flaw in step 2 silently propagates through steps 3–6, producing a confident but incorrect conclusion. The agent then acts on that conclusion without realizing the reasoning was broken.

This is especially dangerous for multi-hop reasoning tasks (math, legal analysis, code debugging) where one bad inference poisons all downstream conclusions.

---

## Option 1: Step-by-Step Consistency Checker

Generate reasoning steps, then re-evaluate each step against prior steps for logical consistency.

```python
import anthropic
import json
import re

client = anthropic.Anthropic()

def extract_steps(reasoning_text: str) -> list[str]:
    """Extract numbered steps from CoT output."""
    lines = reasoning_text.strip().split('\n')
    steps = []
    current_step = []
    for line in lines:
        if re.match(r'^\d+\.', line.strip()):
            if current_step:
                steps.append('\n'.join(current_step).strip())
            current_step = [line]
        elif current_step:
            current_step.append(line)
    if current_step:
        steps.append('\n'.join(current_step).strip())
    return steps

def check_step_consistency(steps: list[str], step_idx: int) -> dict:
    """Check if a step is consistent with all prior steps."""
    if step_idx == 0:
        return {"consistent": True, "issue": None}

    prior_steps = '\n'.join(f"Step {i+1}: {s}" for i, s in enumerate(steps[:step_idx]))
    current_step = steps[step_idx]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"""Given these prior reasoning steps:
{prior_steps}

Is the following step logically consistent with the prior steps?
Step {step_idx + 1}: {current_step}

Reply with JSON only:
{{"consistent": true/false, "issue": "describe the inconsistency or null if consistent"}}"""
        }]
    )
    try:
        return json.loads(response.content[0].text.strip())
    except json.JSONDecodeError:
        return {"consistent": True, "issue": None}

def cot_with_consistency_check(question: str) -> dict:
    """Generate CoT reasoning and verify each step for consistency."""
    # Step 1: Generate chain-of-thought reasoning
    cot_response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"""Solve this step by step, numbering each step:

{question}

Think carefully through each step before moving to the next."""
        }]
    )

    reasoning = cot_response.content[0].text
    steps = extract_steps(reasoning)

    # Step 2: Check consistency of each step
    issues = []
    for i, _ in enumerate(steps):
        result = check_step_consistency(steps, i)
        if not result.get("consistent"):
            issues.append({
                "step": i + 1,
                "step_text": steps[i][:100],
                "issue": result.get("issue")
            })

    # Step 3: If issues found, re-generate with awareness of problems
    if issues:
        issue_summary = '\n'.join(
            f"Step {iss['step']}: {iss['issue']}" for iss in issues
        )
        corrected_response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[
                {"role": "user", "content": f"Solve this step by step:\n{question}"},
                {"role": "assistant", "content": reasoning},
                {"role": "user", "content": f"""Your reasoning has consistency issues:
{issue_summary}

Please redo your reasoning, fixing these specific issues."""}
            ]
        )
        final_reasoning = corrected_response.content[0].text
        verified = False
    else:
        final_reasoning = reasoning
        verified = True

    return {
        "question": question,
        "initial_reasoning": reasoning,
        "consistency_issues": issues,
        "verified": verified,
        "final_reasoning": final_reasoning,
        "steps_checked": len(steps)
    }

# Usage
result = cot_with_consistency_check(
    "A train leaves City A at 9am going 60mph. Another leaves City B at 10am going 80mph. "
    "The cities are 280 miles apart. When do they meet?"
)
print(f"Verified: {result['verified']}")
print(f"Issues found: {len(result['consistency_issues'])}")
print(f"Final reasoning:\n{result['final_reasoning']}")

# Expected Token Savings: Haiku checker uses ~50 tokens/step vs Sonnet re-generation; saves 60-70% on verification vs full re-run
# Environment: math reasoning, multi-hop Q&A, debugging chains
```

---

## Option 2: Logical Entailment Validator

Check whether each conclusion actually follows from its stated premises using entailment analysis.

```python
import anthropic
import json

client = anthropic.Anthropic()

def validate_entailment(premise: str, conclusion: str) -> dict:
    """Check if conclusion logically follows from premise."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": f"""Does the conclusion logically follow from the premise?

Premise: {premise}
Conclusion: {conclusion}

Reply with JSON:
{{"entails": true/false, "confidence": 0.0-1.0, "reason": "brief explanation"}}"""
        }]
    )
    try:
        return json.loads(response.content[0].text.strip())
    except (json.JSONDecodeError, KeyError):
        return {"entails": True, "confidence": 0.5, "reason": "parse error"}

def parse_reasoning_pairs(text: str) -> list[tuple[str, str]]:
    """Extract (premise, conclusion) pairs from reasoning text."""
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    pairs = []
    for i in range(len(lines) - 1):
        if any(lines[i].startswith(f"{n}.") for n in range(1, 20)):
            if any(lines[i+1].startswith(f"{n}.") for n in range(1, 20)):
                pairs.append((lines[i], lines[i+1]))
    return pairs

def cot_entailment_validator(question: str, min_confidence: float = 0.7) -> dict:
    """Run CoT and validate that each step entails the next."""
    # Generate reasoning
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"""Answer this question step by step. For each step, clearly state:
1. What you know (premise)
2. What you conclude (conclusion)

Question: {question}"""
        }]
    )

    reasoning = response.content[0].text
    pairs = parse_reasoning_pairs(reasoning)

    # Validate each entailment
    validation_results = []
    weak_steps = []

    for i, (premise, conclusion) in enumerate(pairs):
        result = validate_entailment(premise, conclusion)
        validation_results.append({
            "step": i + 1,
            "premise": premise[:120],
            "conclusion": conclusion[:120],
            **result
        })
        if not result["entails"] or result["confidence"] < min_confidence:
            weak_steps.append(i + 1)

    # Extract final answer
    final_response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        messages=[
            {"role": "user", "content": question},
            {"role": "assistant", "content": reasoning},
            {"role": "user", "content": "Given your reasoning, what is your final answer? Be concise."}
        ]
    )

    return {
        "question": question,
        "reasoning": reasoning,
        "validation_results": validation_results,
        "weak_steps": weak_steps,
        "entailment_score": sum(1 for r in validation_results if r["entails"]) / max(len(validation_results), 1),
        "answer": final_response.content[0].text,
        "reasoning_sound": len(weak_steps) == 0
    }

# Usage
result = cot_entailment_validator(
    "If all mammals are warm-blooded, and whales are mammals, are whales warm-blooded? "
    "What does this tell us about fish?"
)
print(f"Entailment score: {result['entailment_score']:.1%}")
print(f"Reasoning sound: {result['reasoning_sound']}")
if result["weak_steps"]:
    print(f"Weak steps: {result['weak_steps']}")
print(f"Answer: {result['answer']}")

# Expected Token Savings: Haiku entailment checks (~80 tokens each) vs Sonnet full re-reasoning (~800 tokens); 5x cheaper
# Environment: logical reasoning, syllogisms, formal argument chains
```

---

## Option 3: External Fact Verification

Cross-check factual claims embedded in reasoning steps against a reference knowledge base.

```python
import anthropic
import json
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class FactClaim:
    claim: str
    step_number: int
    verified: bool | None = None
    confidence: float = 0.0
    correction: str | None = None

def extract_factual_claims(reasoning: str) -> list[FactClaim]:
    """Extract verifiable factual claims from reasoning."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"""Extract all verifiable factual claims from this reasoning.
Ignore logical deductions — only extract factual assertions about the world.

Reasoning:
{reasoning}

Reply with JSON array:
[{{"claim": "...", "step_number": 1}}, ...]"""
        }]
    )
    try:
        claims_data = json.loads(response.content[0].text.strip())
        return [FactClaim(**c) for c in claims_data]
    except (json.JSONDecodeError, TypeError):
        return []

def verify_claim(claim: FactClaim, context: str = "") -> FactClaim:
    """Verify a factual claim using LLM knowledge."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"""Is this factual claim accurate?

Claim: {claim.claim}
{f"Context: {context}" if context else ""}

Reply with JSON:
{{"accurate": true/false, "confidence": 0.0-1.0, "correction": "corrected fact or null"}}"""
        }]
    )
    try:
        result = json.loads(response.content[0].text.strip())
        claim.verified = result.get("accurate", True)
        claim.confidence = result.get("confidence", 0.5)
        claim.correction = result.get("correction")
    except json.JSONDecodeError:
        claim.verified = True
        claim.confidence = 0.5
    return claim

def cot_with_fact_verification(question: str, context: str = "") -> dict:
    """Generate CoT reasoning and verify all factual claims within it."""
    # Generate reasoning
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"""{f"Context: {context}" + chr(10) if context else ""}Question: {question}

Think through this step by step, citing any facts you rely on."""
        }]
    )

    reasoning = response.content[0].text

    # Extract and verify facts
    claims = extract_factual_claims(reasoning)
    verified_claims = [verify_claim(c, context) for c in claims]

    incorrect = [c for c in verified_claims if not c.verified and c.confidence > 0.7]

    # If incorrect facts found, regenerate with corrections
    if incorrect:
        corrections = '\n'.join(
            f"- Step {c.step_number}: '{c.claim}' is incorrect. Correction: {c.correction}"
            for c in incorrect if c.correction
        )
        corrected_response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[
                {"role": "user", "content": f"Question: {question}\n\nThink through this step by step."},
                {"role": "assistant", "content": reasoning},
                {"role": "user", "content": f"""The following facts in your reasoning are incorrect:
{corrections}

Please redo your reasoning with the correct facts."""}
            ]
        )
        final_reasoning = corrected_response.content[0].text
    else:
        final_reasoning = reasoning

    return {
        "question": question,
        "reasoning": reasoning,
        "claims_checked": len(verified_claims),
        "incorrect_claims": [
            {"claim": c.claim, "step": c.step_number, "correction": c.correction}
            for c in incorrect
        ],
        "fact_accuracy": sum(1 for c in verified_claims if c.verified) / max(len(verified_claims), 1),
        "final_reasoning": final_reasoning
    }

# Usage
result = cot_with_fact_verification(
    "Why did the Roman Empire fall, and what were the key dates involved?",
    context="Focus on Western Roman Empire"
)
print(f"Claims checked: {result['claims_checked']}")
print(f"Fact accuracy: {result['fact_accuracy']:.1%}")
if result["incorrect_claims"]:
    print(f"Incorrect claims found: {len(result['incorrect_claims'])}")
    for c in result["incorrect_claims"]:
        print(f"  Step {c['step']}: {c['correction']}")

# Expected Token Savings: Haiku fact-checker (~100 tokens/claim) prevents full Sonnet redo in 70% of cases
# Environment: historical Q&A, scientific reasoning, domain-specific knowledge tasks
```

---

## Option 4: Contradiction Detector

Identify when later steps contradict earlier established conclusions.

```python
import anthropic
import json
from itertools import combinations

client = anthropic.Anthropic()

def detect_contradictions(statements: list[str]) -> list[dict]:
    """Check all pairs of statements for contradictions."""
    contradictions = []

    # Batch check pairs to reduce API calls
    pairs_to_check = list(combinations(enumerate(statements), 2))

    # Check up to 10 pairs to avoid excessive calls
    for (i, stmt_a), (j, stmt_b) in pairs_to_check[:10]:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{
                "role": "user",
                "content": f"""Do these two statements contradict each other?

Statement A (step {i+1}): {stmt_a}
Statement B (step {j+1}): {stmt_b}

Reply with JSON: {{"contradicts": true/false, "explanation": "why or null"}}"""
            }]
        )
        try:
            result = json.loads(response.content[0].text.strip())
            if result.get("contradicts"):
                contradictions.append({
                    "step_a": i + 1,
                    "step_b": j + 1,
                    "statement_a": stmt_a[:100],
                    "statement_b": stmt_b[:100],
                    "explanation": result.get("explanation")
                })
        except json.JSONDecodeError:
            continue

    return contradictions

def extract_conclusions(reasoning: str) -> list[str]:
    """Pull out the main conclusion/claim from each step."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": f"""Extract the main conclusion or claim from each step in this reasoning.
One sentence per step.

Reasoning:
{reasoning}

Reply with JSON: ["conclusion from step 1", "conclusion from step 2", ...]"""
        }]
    )
    try:
        return json.loads(response.content[0].text.strip())
    except json.JSONDecodeError:
        return []

def cot_contradiction_detector(question: str) -> dict:
    """Generate CoT and detect any self-contradictions in the reasoning."""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"""Think through this carefully, step by step:

{question}

Make sure each step builds consistently on the previous ones."""
        }]
    )

    reasoning = response.content[0].text
    conclusions = extract_conclusions(reasoning)
    contradictions = detect_contradictions(conclusions)

    resolved_reasoning = reasoning
    if contradictions:
        contradiction_desc = '\n'.join(
            f"- Steps {c['step_a']} and {c['step_b']} contradict: {c['explanation']}"
            for c in contradictions
        )

        resolution_response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[
                {"role": "user", "content": f"Think through this step by step:\n{question}"},
                {"role": "assistant", "content": reasoning},
                {"role": "user", "content": f"""Your reasoning contains contradictions:
{contradiction_desc}

Please re-reason from scratch, resolving these contradictions."""}
            ]
        )
        resolved_reasoning = resolution_response.content[0].text

    return {
        "question": question,
        "original_reasoning": reasoning,
        "conclusions_extracted": conclusions,
        "contradictions": contradictions,
        "contradiction_free": len(contradictions) == 0,
        "final_reasoning": resolved_reasoning
    }

# Usage
result = cot_contradiction_detector(
    "Is it better to invest in index funds or individual stocks? "
    "Consider both risk tolerance and long-term returns."
)
print(f"Contradiction-free: {result['contradiction_free']}")
if not result['contradiction_free']:
    print(f"Found {len(result['contradictions'])} contradiction(s):")
    for c in result["contradictions"]:
        print(f"  Steps {c['step_a']} vs {c['step_b']}: {c['explanation']}")

# Expected Token Savings: Early contradiction detection prevents downstream compounding errors; saves full re-answer cost
# Environment: comparative analysis, argument construction, multi-perspective reasoning
```

---

## Option 5: Confidence-Gated Chain-of-Thought

Only commit to the final answer when every reasoning step exceeds a minimum confidence threshold.

```python
import anthropic
import json
import re

client = anthropic.Anthropic()

def generate_cot_with_confidence(question: str) -> tuple[str, list[dict]]:
    """Generate CoT where each step includes an explicit confidence score."""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        messages=[{
            "role": "user",
            "content": f"""Answer this question step by step.
After each step, rate your confidence in that step: [CONF: X%]

Question: {question}

Format each step as:
N. [reasoning] [CONF: X%]"""
        }]
    )
    reasoning = response.content[0].text

    # Parse confidence scores
    steps = []
    for match in re.finditer(r'(\d+)\.\s*(.*?)\s*\[CONF:\s*(\d+)%\]', reasoning, re.DOTALL):
        step_num = int(match.group(1))
        step_text = match.group(2).strip()
        confidence = int(match.group(3))
        steps.append({
            "step": step_num,
            "text": step_text,
            "confidence": confidence
        })

    return reasoning, steps

def request_clarification_for_step(question: str, step: dict, prior_reasoning: str) -> str:
    """Ask for more careful reasoning on a low-confidence step."""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[
            {"role": "user", "content": f"Solve this step by step:\n{question}"},
            {"role": "assistant", "content": prior_reasoning},
            {"role": "user", "content": f"""Step {step['step']} had low confidence ({step['confidence']}%):
"{step['text']}"

Please reconsider this step more carefully. What additional analysis or alternative approaches should be considered?"""}
        ]
    )
    return response.content[0].text

def confidence_gated_cot(question: str, min_confidence: int = 70) -> dict:
    """Run CoT and gate the final answer on all steps meeting confidence threshold."""
    reasoning, steps = generate_cot_with_confidence(question)

    low_confidence_steps = [s for s in steps if s["confidence"] < min_confidence]
    clarifications = {}

    for step in low_confidence_steps:
        clarification = request_clarification_for_step(question, step, reasoning)
        clarifications[step["step"]] = clarification

    # Generate final answer incorporating clarifications
    if clarifications:
        clarification_text = '\n'.join(
            f"Revised analysis for step {k}:\n{v}"
            for k, v in clarifications.items()
        )
        final_response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[
                {"role": "user", "content": question},
                {"role": "assistant", "content": reasoning},
                {"role": "user", "content": f"""Based on this additional analysis:
{clarification_text}

What is your final answer?"""}
            ]
        )
        final_answer = final_response.content[0].text
        all_steps_confident = False
    else:
        # Extract conclusion directly
        final_response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[
                {"role": "user", "content": question},
                {"role": "assistant", "content": reasoning},
                {"role": "user", "content": "Summarize your final answer in 1-2 sentences."}
            ]
        )
        final_answer = final_response.content[0].text
        all_steps_confident = True

    avg_confidence = sum(s["confidence"] for s in steps) / max(len(steps), 1)

    return {
        "question": question,
        "reasoning": reasoning,
        "steps": steps,
        "low_confidence_steps": [s["step"] for s in low_confidence_steps],
        "clarifications": clarifications,
        "avg_confidence": avg_confidence,
        "all_steps_confident": all_steps_confident,
        "final_answer": final_answer
    }

# Usage
result = confidence_gated_cot(
    "What are the main causes of inflation, and which policy tools most effectively counter it?",
    min_confidence=75
)
print(f"Average step confidence: {result['avg_confidence']:.0f}%")
print(f"Low-confidence steps: {result['low_confidence_steps']}")
print(f"All steps confident: {result['all_steps_confident']}")
print(f"\nFinal answer:\n{result['final_answer']}")

# Expected Token Savings: Only low-confidence steps trigger extra calls; saves 40-80% vs always re-reasoning
# Environment: expert Q&A, financial analysis, medical reasoning, any high-stakes decision
```

---

## Option 6: Self-Critique Loop

After generating reasoning, ask the model to critique its own chain-of-thought and regenerate if weaknesses are found.

```python
import anthropic
import json

client = anthropic.Anthropic()

def critique_reasoning(question: str, reasoning: str) -> dict:
    """Ask the model to critically evaluate its own reasoning."""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"""You previously answered this question with the following reasoning.
Critically evaluate the quality of your reasoning.

Question: {question}

Your reasoning:
{reasoning}

Evaluate on:
1. Logical soundness — do conclusions follow from premises?
2. Completeness — are important considerations missing?
3. Assumptions — are any assumptions unjustified?
4. Alternative views — were relevant alternatives considered?

Reply with JSON:
{{
  "overall_quality": "high/medium/low",
  "weaknesses": ["weakness 1", "weakness 2"],
  "missing_considerations": ["consideration 1"],
  "should_regenerate": true/false,
  "regeneration_guidance": "what to do differently"
}}"""
        }]
    )
    try:
        return json.loads(response.content[0].text.strip())
    except json.JSONDecodeError:
        return {
            "overall_quality": "high",
            "weaknesses": [],
            "missing_considerations": [],
            "should_regenerate": False,
            "regeneration_guidance": ""
        }

def self_critique_cot(question: str, max_iterations: int = 2) -> dict:
    """Generate CoT with self-critique loop until quality is satisfactory."""
    history = []
    current_reasoning = None

    for iteration in range(max_iterations + 1):
        # Generate reasoning
        messages = [{"role": "user", "content": f"Think through this step by step:\n{question}"}]

        if current_reasoning and history:
            last_critique = history[-1]["critique"]
            messages[0]["content"] += f"""

Previous attempt had these weaknesses:
{chr(10).join('- ' + w for w in last_critique.get('weaknesses', []))}
{chr(10).join('- ' + c for c in last_critique.get('missing_considerations', []))}

Guidance: {last_critique.get('regeneration_guidance', '')}

Please address these issues in your new reasoning."""

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=messages
        )
        current_reasoning = response.content[0].text

        # Critique the reasoning
        if iteration < max_iterations:
            critique = critique_reasoning(question, current_reasoning)
        else:
            # Final iteration — accept as-is
            critique = {
                "overall_quality": "accepted",
                "should_regenerate": False,
                "weaknesses": [],
                "missing_considerations": [],
                "regeneration_guidance": ""
            }

        history.append({
            "iteration": iteration + 1,
            "reasoning_preview": current_reasoning[:200],
            "critique": critique
        })

        if not critique.get("should_regenerate", False):
            break

    # Generate final concise answer from best reasoning
    final_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[
            {"role": "user", "content": question},
            {"role": "assistant", "content": current_reasoning},
            {"role": "user", "content": "Give a clear, direct final answer based on your reasoning."}
        ]
    )

    return {
        "question": question,
        "iterations": len(history),
        "history": history,
        "final_reasoning": current_reasoning,
        "final_answer": final_response.content[0].text,
        "quality": history[-1]["critique"].get("overall_quality", "unknown")
    }

# Usage
result = self_critique_cot(
    "Should a startup prioritize growth or profitability in its first 3 years?",
    max_iterations=2
)
print(f"Iterations taken: {result['iterations']}")
print(f"Final quality: {result['quality']}")
for h in result["history"]:
    print(f"\nIteration {h['iteration']}:")
    print(f"  Weaknesses: {h['critique'].get('weaknesses', [])}")
    print(f"  Should regenerate: {h['critique'].get('should_regenerate', False)}")
print(f"\nFinal answer:\n{result['final_answer']}")

# Expected Token Savings: Bounded iteration (max 2) prevents infinite loops; self-critique on Sonnet cheaper than human review
# Environment: strategy questions, open-ended analysis, advisory agents, autonomous research
```

---

## Comparison

| Option | Verification Type | Model Used | Latency | Best For |
|--------|------------------|------------|---------|----------|
| 1. Step Consistency | Each step vs prior steps | Haiku checker | Medium | Sequential reasoning chains |
| 2. Entailment Validator | Premise→conclusion links | Haiku entailment | Medium | Formal logical arguments |
| 3. Fact Verification | Factual claim accuracy | Haiku fact-check | Medium | Knowledge-heavy reasoning |
| 4. Contradiction Detector | Cross-step contradictions | Haiku pairwise | Medium | Multi-perspective analysis |
| 5. Confidence-Gated | Self-rated step confidence | Sonnet inline | Low-High | High-stakes decisions |
| 6. Self-Critique Loop | Holistic quality critique | Sonnet critique | High | Open-ended analysis |

**Recommended defaults:**
- **Math/logic tasks** → Option 1 (consistency) or Option 2 (entailment)
- **Factual Q&A** → Option 3 (fact verification)
- **Strategy/analysis** → Option 6 (self-critique loop)
- **General reasoning** → Option 5 (confidence-gated, lowest overhead)
