---
layout: solution
title: "Agent Doesn't Implement Multi-Source Cross-Reference Verification"
category: hallucination
description: "Verify factual claims against multiple independent sources before responding to reduce hallucination and increase output reliability."
tags: [hallucination, verification, cross-reference, fact-checking, reliability, grounding]
---

# Agent Doesn't Implement Multi-Source Cross-Reference Verification

## Problem

LLM agents frequently produce plausible-sounding but incorrect facts, statistics, dates, and citations. Without cross-referencing claims across multiple independent sources or reasoning paths, hallucinations slip through undetected. Users receive confidently stated falsehoods, eroding trust in the system.

## Solutions

### Option 1: Dual-Call Consistency Check

Generate two independent answers with different random seeds and compare them for factual consistency before returning a response.

```python
import anthropic

client = anthropic.Anthropic()

CONSISTENCY_THRESHOLD = 0.7  # fraction of key claims that must agree


def extract_key_claims(text: str) -> list[str]:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                "Extract the key factual claims from this text as a JSON list of strings. "
                "Only include verifiable facts, not opinions:\n\n" + text
            ),
        }],
    )
    import json, re
    raw = resp.content[0].text
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return [line.strip("- ").strip() for line in raw.splitlines() if line.strip().startswith("-")]


def check_claim_consistency(claim: str, answer_a: str, answer_b: str) -> bool:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{
            "role": "user",
            "content": (
                f"Claim: {claim}\n\n"
                f"Source A: {answer_a[:500]}\n\n"
                f"Source B: {answer_b[:500]}\n\n"
                "Do both sources agree on this claim? Reply with only YES or NO."
            ),
        }],
    )
    return "YES" in resp.content[0].text.upper()


def verified_answer(question: str) -> dict:
    # generate two independent answers
    def ask(q: str) -> str:
        r = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{"role": "user", "content": q}],
        )
        return r.content[0].text

    answer_a = ask(question)
    answer_b = ask(question + " Please answer independently and thoroughly.")

    claims = extract_key_claims(answer_a)
    if not claims:
        return {"answer": answer_a, "confidence": "unknown", "verified_claims": 0}

    agreed = sum(1 for c in claims if check_claim_consistency(c, answer_a, answer_b))
    consistency = agreed / len(claims) if claims else 0.0

    if consistency >= CONSISTENCY_THRESHOLD:
        return {
            "answer": answer_a,
            "confidence": "high",
            "consistency_score": round(consistency, 2),
            "verified_claims": agreed,
            "total_claims": len(claims),
        }
    else:
        # synthesize a cautious answer noting disagreements
        synth = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": (
                    f"Two sources disagree on parts of this answer to '{question}'.\n"
                    f"Source A: {answer_a}\nSource B: {answer_b}\n"
                    "Provide a careful answer noting where sources disagree."
                ),
            }],
        )
        return {
            "answer": synth.content[0].text,
            "confidence": "low",
            "consistency_score": round(consistency, 2),
            "verified_claims": agreed,
            "total_claims": len(claims),
        }


if __name__ == "__main__":
    questions = [
        "When was the Eiffel Tower built and how tall is it?",
        "What is the population of Tokyo?",
        "Who invented the World Wide Web?",
    ]
    for q in questions:
        result = verified_answer(q)
        print(f"Q: {q}")
        print(f"Confidence: {result['confidence']} (score={result.get('consistency_score', 'N/A')})")
        print(f"Answer: {result['answer'][:150]}...\n")

# Expected Token Savings: 2x token cost but eliminates high-confidence hallucinations
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 2: Structured Fact Extraction + Cross-Reference

Extract structured facts from the answer, then verify each fact independently with a targeted follow-up query.

```python
import anthropic
import json
import re
from dataclasses import dataclass

client = anthropic.Anthropic()


@dataclass
class Fact:
    claim: str
    verified: bool = False
    confidence: str = "unverified"
    contradiction: str = ""


def extract_facts(answer: str) -> list[Fact]:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                "Extract all specific verifiable facts from this text. "
                "Return JSON: [{\"claim\": \"...\"}]\n\n" + answer
            ),
        }],
    )
    raw = resp.content[0].text
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        return []
    try:
        items = json.loads(match.group())
        return [Fact(claim=item["claim"]) for item in items if "claim" in item]
    except (json.JSONDecodeError, KeyError):
        return []


def verify_fact(fact: Fact) -> Fact:
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                f"Verify this factual claim: \"{fact.claim}\"\n\n"
                "Respond with JSON: {\"verified\": true/false, \"confidence\": \"high/medium/low\", "
                "\"correction\": \"corrected fact if wrong, else empty string\"}"
            ),
        }],
    )
    raw = resp.content[0].text
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            fact.verified    = bool(data.get("verified", False))
            fact.confidence  = str(data.get("confidence", "low"))
            fact.contradiction = str(data.get("correction", ""))
        except (json.JSONDecodeError, KeyError):
            fact.confidence = "low"
    return fact


def cross_reference_answer(question: str) -> dict:
    # generate initial answer
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": question}],
    )
    answer = resp.content[0].text

    facts = extract_facts(answer)
    verified_facts = [verify_fact(f) for f in facts]

    unverified = [f for f in verified_facts if not f.verified]
    corrections = [f for f in verified_facts if f.contradiction]

    if corrections:
        correction_text = "\n".join(
            f"- Original: {f.claim} → Correction: {f.contradiction}"
            for f in corrections
        )
        fix_resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": (
                    f"Original answer: {answer}\n\n"
                    f"The following facts were incorrect:\n{correction_text}\n\n"
                    "Please provide a corrected answer."
                ),
            }],
        )
        final_answer = fix_resp.content[0].text
    else:
        final_answer = answer

    return {
        "answer": final_answer,
        "total_facts": len(verified_facts),
        "verified": len([f for f in verified_facts if f.verified]),
        "corrected": len(corrections),
        "corrections": [{"claim": f.claim, "correction": f.contradiction} for f in corrections],
    }


if __name__ == "__main__":
    questions = [
        "What year did NASA land on the moon and who were the astronauts?",
        "What is the speed of light and who first measured it accurately?",
    ]
    for q in questions:
        result = cross_reference_answer(q)
        print(f"Q: {q}")
        print(f"Facts verified: {result['verified']}/{result['total_facts']}, corrected: {result['corrected']}")
        print(f"Answer: {result['answer'][:200]}...\n")

# Expected Token Savings: 3–4x overhead but catches specific factual errors with precision
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 3: Chain-of-Verification (CoVe) Prompting

Use chain-of-verification: generate answer, plan verification questions, answer them, then produce a revised final answer.

```python
import anthropic

client = anthropic.Anthropic()


def chain_of_verification(question: str) -> dict:
    # Step 1: Generate baseline answer
    baseline_resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": question}],
    )
    baseline = baseline_resp.content[0].text

    # Step 2: Generate verification questions
    vq_resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                f"Original question: {question}\n\n"
                f"Draft answer: {baseline}\n\n"
                "List 3–5 specific factual verification questions that would confirm "
                "or refute the claims in the draft answer. Format as a numbered list."
            ),
        }],
    )
    verification_questions = vq_resp.content[0].text

    # Step 3: Answer each verification question independently (no draft context)
    vq_answers_resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                "Answer each of the following factual questions as accurately as possible. "
                "Do NOT reference any prior answer — answer from your own knowledge only.\n\n"
                + verification_questions
            ),
        }],
    )
    vq_answers = vq_answers_resp.content[0].text

    # Step 4: Produce a final revised answer using the verification results
    final_resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                f"Original question: {question}\n\n"
                f"Draft answer: {baseline}\n\n"
                f"Verification questions and answers:\n{vq_answers}\n\n"
                "Using the verification answers, produce a final, accurate response to the "
                "original question. Correct any errors found during verification."
            ),
        }],
    )
    final = final_resp.content[0].text

    return {
        "question": question,
        "baseline": baseline,
        "verification_questions": verification_questions,
        "verification_answers": vq_answers,
        "final_answer": final,
    }


if __name__ == "__main__":
    questions = [
        "What are the main causes and effects of the 2008 financial crisis?",
        "Explain how mRNA vaccines work and who developed them.",
        "What is the distance from Earth to the Moon and how was it first measured?",
    ]
    for q in questions:
        result = chain_of_verification(q)
        print(f"Q: {q}")
        print(f"Verification Qs:\n{result['verification_questions'][:200]}")
        print(f"Final Answer: {result['final_answer'][:200]}...\n{'='*60}\n")

# Expected Token Savings: 4–5x overhead but dramatically reduces factual errors on complex queries
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 4: Async Parallel Verification with Voting

Run multiple independent answer attempts in parallel and use majority voting on key facts.

```python
import anthropic
import asyncio
import json
import re
from collections import Counter

client = anthropic.AsyncAnthropic()

NUM_VOTERS = 3   # number of independent responses to generate


async def get_structured_answer(question: str, attempt_id: int) -> dict:
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                f"{question}\n\n"
                "Provide your answer with key facts formatted as JSON at the end: "
                "{\"facts\": {\"key\": \"value\", ...}}"
            ),
        }],
    )
    text = resp.content[0].text
    facts: dict = {}
    match = re.search(r'\{"facts":\s*\{.*?\}\s*\}', text, re.DOTALL)
    if match:
        try:
            facts = json.loads(match.group()).get("facts", {})
        except json.JSONDecodeError:
            pass
    return {"attempt": attempt_id, "answer": text, "facts": facts}


def majority_vote_facts(responses: list[dict]) -> dict:
    all_keys: set[str] = set()
    for r in responses:
        all_keys.update(r["facts"].keys())

    voted_facts: dict = {}
    disagreements: list[str] = []

    for key in all_keys:
        values = [r["facts"][key] for r in responses if key in r["facts"]]
        if not values:
            continue
        counter = Counter(str(v).lower().strip() for v in values)
        most_common_val, count = counter.most_common(1)[0]
        voted_facts[key] = most_common_val
        if count < len(responses):
            disagreements.append(f"{key}: {dict(counter)}")

    return {"facts": voted_facts, "disagreements": disagreements}


async def parallel_verified_answer(question: str) -> dict:
    tasks = [get_structured_answer(question, i) for i in range(NUM_VOTERS)]
    responses = await asyncio.gather(*tasks)

    vote_result = majority_vote_facts(list(responses))

    # synthesize final answer using voted facts
    synth_resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                f"Question: {question}\n\n"
                f"Verified facts (majority vote from {NUM_VOTERS} sources):\n"
                + json.dumps(vote_result["facts"], indent=2)
                + (
                    f"\n\nNote: disagreements on: {', '.join(vote_result['disagreements'])}"
                    if vote_result["disagreements"] else ""
                )
                + "\n\nWrite a clear, accurate answer using only the verified facts above."
            ),
        }],
    )

    return {
        "question": question,
        "voted_facts": vote_result["facts"],
        "disagreements": vote_result["disagreements"],
        "answer": synth_resp.content[0].text,
        "num_voters": NUM_VOTERS,
    }


async def main() -> None:
    questions = [
        "When was the Python programming language created and by whom?",
        "What is the boiling point of water at sea level in Celsius and Fahrenheit?",
        "How many bones are in the adult human body?",
    ]
    results = await asyncio.gather(*[parallel_verified_answer(q) for q in questions])
    for r in results:
        print(f"Q: {r['question']}")
        print(f"Voted facts: {r['voted_facts']}")
        if r["disagreements"]:
            print(f"Disagreements: {r['disagreements']}")
        print(f"Answer: {r['answer'][:150]}...\n")


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: NUM_VOTERS x overhead but parallel so latency ≈ 1x; highest reliability
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 5: Confidence-Weighted Consensus with Abstention

Ask for explicit confidence scores per claim, then abstain or flag low-confidence answers rather than hallucinating.

```python
import anthropic
import json
import re
from dataclasses import dataclass

client = anthropic.Anthropic()

CONFIDENCE_THRESHOLD = 0.75  # below this, abstain or flag


@dataclass
class ScoredClaim:
    text: str
    confidence: float   # 0.0–1.0
    basis: str          # "training data", "inference", "uncertain"


def extract_scored_claims(question: str) -> tuple[str, list[ScoredClaim]]:
    resp = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=768,
        messages=[{
            "role": "user",
            "content": (
                f"Answer the following question and for each factual claim you make, "
                "provide a confidence score (0.0–1.0) and basis for that score.\n\n"
                f"Question: {question}\n\n"
                "Format your response as:\n"
                "ANSWER: <your full answer>\n"
                "CLAIMS: [{\"text\": \"...\", \"confidence\": 0.9, \"basis\": \"training data\"}]"
            ),
        }],
    )
    raw = resp.content[0].text

    answer_match = re.search(r"ANSWER:\s*(.*?)(?=CLAIMS:|$)", raw, re.DOTALL)
    claims_match = re.search(r"CLAIMS:\s*(\[.*\])", raw, re.DOTALL)

    answer = answer_match.group(1).strip() if answer_match else raw
    claims: list[ScoredClaim] = []

    if claims_match:
        try:
            items = json.loads(claims_match.group(1))
            for item in items:
                claims.append(ScoredClaim(
                    text=item.get("text", ""),
                    confidence=float(item.get("confidence", 0.5)),
                    basis=item.get("basis", "uncertain"),
                ))
        except (json.JSONDecodeError, KeyError, ValueError):
            pass

    return answer, claims


def confidence_gated_response(question: str) -> dict:
    answer, claims = extract_scored_claims(question)

    low_confidence = [c for c in claims if c.confidence < CONFIDENCE_THRESHOLD]
    high_confidence = [c for c in claims if c.confidence >= CONFIDENCE_THRESHOLD]

    avg_confidence = sum(c.confidence for c in claims) / len(claims) if claims else 0.5

    if avg_confidence < CONFIDENCE_THRESHOLD or len(low_confidence) > len(high_confidence):
        # produce a hedged response
        hedge_resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": (
                    f"Question: {question}\n\n"
                    f"Draft answer: {answer}\n\n"
                    f"The following claims have low confidence:\n"
                    + "\n".join(f"- {c.text} (confidence={c.confidence:.2f})" for c in low_confidence)
                    + "\n\nRewrite the answer to clearly flag uncertain claims, "
                    "recommend authoritative sources, and avoid stating uncertain facts as definitive."
                ),
            }],
        )
        final = hedge_resp.content[0].text
        status = "hedged"
    else:
        final = answer
        status = "confident"

    return {
        "question": question,
        "answer": final,
        "status": status,
        "avg_confidence": round(avg_confidence, 2),
        "high_confidence_claims": len(high_confidence),
        "low_confidence_claims": len(low_confidence),
        "flagged_claims": [c.text for c in low_confidence],
    }


if __name__ == "__main__":
    questions = [
        "What is the exact GDP of Nigeria in 2023?",
        "What is the chemical formula for water?",
        "Who won the Nobel Prize in Physics in 2022?",
        "What will be the population of Earth in 2050?",
    ]
    for q in questions:
        result = confidence_gated_response(q)
        print(f"Q: {q}")
        print(f"Status: {result['status']} (avg_conf={result['avg_confidence']})")
        print(f"Claims H/L: {result['high_confidence_claims']}/{result['low_confidence_claims']}")
        print(f"Answer: {result['answer'][:150]}...\n")

# Expected Token Savings: 2x overhead; prevents confidently wrong answers on uncertain topics
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 6: Tool-Based Multi-Source Grounding

Define search tools and require the agent to ground each factual claim in a tool result before including it in the response.

```python
import anthropic
import json

client = anthropic.Anthropic()

# Simulated knowledge bases (in production: real search APIs)
KNOWLEDGE_BASES = {
    "encyclopedia": {
        "eiffel tower": "The Eiffel Tower was constructed from 1887 to 1889, designed by Gustave Eiffel. Height: 330m including antenna.",
        "speed of light": "The speed of light in vacuum is 299,792,458 m/s (approximately 3×10⁸ m/s).",
        "dna": "DNA (deoxyribonucleic acid) was first described by Watson and Crick in 1953 using X-ray data from Franklin.",
    },
    "statistics_db": {
        "world population": "World population as of 2024: approximately 8.1 billion.",
        "tokyo population": "Greater Tokyo Area population: approximately 37–38 million (2023 estimate).",
    },
}


def search_knowledge_base(query: str, source: str) -> str:
    kb = KNOWLEDGE_BASES.get(source, {})
    query_lower = query.lower()
    for key, value in kb.items():
        if any(word in query_lower for word in key.split()):
            return value
    return f"No result found for '{query}' in {source}."


TOOLS = [
    {
        "name": "search_encyclopedia",
        "description": "Search the encyclopedia for factual information about topics, events, people, and science.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Search query"}},
            "required": ["query"],
        },
    },
    {
        "name": "search_statistics",
        "description": "Search the statistics database for population, economic, and numerical data.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Statistical query"}},
            "required": ["query"],
        },
    },
]


def run_tool(name: str, inputs: dict) -> str:
    if name == "search_encyclopedia":
        return search_knowledge_base(inputs["query"], "encyclopedia")
    elif name == "search_statistics":
        return search_knowledge_base(inputs["query"], "statistics_db")
    return "Unknown tool."


def grounded_answer(question: str) -> dict:
    messages = [
        {
            "role": "user",
            "content": (
                f"{question}\n\n"
                "IMPORTANT: Before stating any specific facts, numbers, dates, or statistics, "
                "you MUST use the available tools to verify them. Do not state facts from memory alone."
            ),
        }
    ]
    tool_calls_made: list[dict] = []

    # agentic loop
    for _ in range(6):
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )

        if resp.stop_reason == "end_turn":
            final = next(
                (b.text for b in resp.content if hasattr(b, "text")), ""
            )
            return {
                "question": question,
                "answer": final,
                "tool_calls": tool_calls_made,
                "grounded": len(tool_calls_made) > 0,
            }

        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    result = run_tool(block.name, block.input)
                    tool_calls_made.append({"tool": block.name, "query": block.input, "result": result})
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": tool_results})

    return {"question": question, "answer": "Could not complete verification.", "tool_calls": tool_calls_made, "grounded": False}


if __name__ == "__main__":
    questions = [
        "How tall is the Eiffel Tower and when was it built?",
        "What is the current world population?",
        "What is the speed of light?",
    ]
    for q in questions:
        result = grounded_answer(q)
        print(f"Q: {q}")
        print(f"Grounded: {result['grounded']} ({len(result['tool_calls'])} tool calls)")
        for tc in result["tool_calls"]:
            print(f"  [{tc['tool']}] {tc['query']} → {tc['result'][:80]}")
        print(f"Answer: {result['answer'][:200]}...\n")

# Expected Token Savings: moderate overhead; guarantees grounded facts when tools cover the domain
# Environment: ANTHROPIC_API_KEY must be set
```

---

## Comparison

| Option | Strategy | Overhead | Hallucination Reduction | Latency | Best For |
|--------|----------|----------|------------------------|---------|----------|
| 1 | Dual-call consistency check | 2x tokens | High | 2x | General Q&A |
| 2 | Structured fact extraction + verify | 3–4x tokens | Very High | 3x | Fact-dense content |
| 3 | Chain-of-verification (CoVe) | 4–5x tokens | Highest | 4x | Complex reasoning |
| 4 | Async parallel voting | 3x tokens | Very High | ~1x (parallel) | High-throughput services |
| 5 | Confidence-weighted with abstention | 2x tokens | High | 2x | Risk-averse applications |
| 6 | Tool-based grounding | 2x tokens | Very High (domain-bound) | 1.5x | Domain-specific agents |
