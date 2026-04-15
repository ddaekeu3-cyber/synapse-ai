---
layout: solution
title: "Agent Doesn't Implement Chain-of-Verification Prompting"
category: prompt-engineering
description: "Generate an initial answer, derive targeted verification questions, answer each independently, then produce a final corrected response — reducing hallucination and factual errors."
tags: [prompt-engineering, verification, hallucination, chain-of-thought, accuracy, python]
---

# Agent Doesn't Implement Chain-of-Verification Prompting

Agents that answer in one shot have no mechanism to catch their own errors. Chain-of-Verification (CoVe) fixes this: generate a draft, identify the specific claims that could be wrong, verify each claim independently, then produce a corrected final answer. This dramatically reduces factual errors without requiring external knowledge sources.

## Option 1: Basic Three-Step CoVe Pipeline

```python
import anthropic

client = anthropic.Anthropic()

def generate_draft(question: str) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": question}],
    )
    return resp.content[0].text

def generate_verification_questions(question: str, draft: str) -> list[str]:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content":
            f"Original question: {question}\n\nDraft answer:\n{draft}\n\n"
            "List 3 specific factual claims in this answer that could be wrong. "
            "For each, write one verification question. "
            "Format: one question per line, no numbering."}],
    )
    lines = [l.strip() for l in resp.content[0].text.split("\n") if l.strip() and "?" in l]
    return lines[:3]

def verify_claim(question: str) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content":
            f"Answer this factual question accurately and concisely: {question}"}],
    )
    return resp.content[0].text.strip()

def generate_final_answer(original: str, draft: str, verifications: list[tuple]) -> str:
    ver_text = "\n".join(f"Q: {q}\nA: {a}" for q, a in verifications)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content":
            f"Original question: {original}\n\n"
            f"Draft answer:\n{draft}\n\n"
            f"Verification results:\n{ver_text}\n\n"
            "Using the verified facts, write a corrected final answer. "
            "Fix any errors found in the draft."}],
    )
    return resp.content[0].text

def chain_of_verification(question: str) -> str:
    print("Step 1: Draft...")
    draft = generate_draft(question)
    print(f"  {draft[:100]}")

    print("Step 2: Verification questions...")
    vqs = generate_verification_questions(question, draft)
    for q in vqs:
        print(f"  ? {q}")

    print("Step 3: Verify each claim...")
    verifications = []
    for vq in vqs:
        answer = verify_claim(vq)
        verifications.append((vq, answer))
        print(f"  A: {answer[:60]}")

    print("Step 4: Final corrected answer...")
    final = generate_final_answer(question, draft, verifications)
    return final

question = "What year was Python created and who created it? What was its first stable release?"
result = chain_of_verification(question)
print(f"\nFinal answer:\n{result}")

# Expected Token Savings: 4 Haiku calls beat 1 Opus call for accuracy at lower cost
# Environment: pure Python; all 4 steps use Haiku; upgrade to Sonnet for critical-path accuracy
```

## Option 2: Parallel Verification with asyncio

```python
import anthropic
import asyncio

client = anthropic.AsyncAnthropic()

async def draft(question: str) -> str:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": question}],
    )
    return resp.content[0].text

async def extract_claims(question: str, draft_text: str) -> list[str]:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content":
            f"Draft answer to '{question}':\n{draft_text}\n\n"
            "Extract 4 specific factual claims as verification questions. "
            "One per line, no numbering."}],
    )
    return [l.strip() for l in resp.content[0].text.split("\n")
            if l.strip() and "?" in l][:4]

async def verify_one(claim_q: str) -> tuple[str, str]:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=96,
        messages=[{"role": "user", "content": f"Answer accurately: {claim_q}"}],
    )
    return claim_q, resp.content[0].text.strip()

async def final_answer(question: str, draft_text: str,
                        verifications: list[tuple]) -> str:
    ver_text = "\n".join(f"Claim: {q}\nFact: {a}" for q, a in verifications)
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content":
            f"Question: {question}\nDraft:\n{draft_text}\n\n"
            f"Verified facts:\n{ver_text}\n\n"
            "Write the corrected final answer incorporating verified facts."}],
    )
    return resp.content[0].text

async def cove_parallel(question: str) -> str:
    draft_text = await draft(question)
    claims = await extract_claims(question, draft_text)
    # Verify all claims in parallel
    verifications = await asyncio.gather(*[verify_one(c) for c in claims])
    return await final_answer(question, draft_text, list(verifications))

question = "Who invented the telephone and what year? Who invented radio?"
result = asyncio.run(cove_parallel(question))
print(f"Q: {question}\nA: {result}")

# Expected Token Savings: Parallel verification cuts wall-clock time by N×; same token cost
# Environment: asyncio; all calls use Haiku; parallel verification is safe since claims are independent
```

## Option 3: Domain-Specific Verification with Confidence Scoring

```python
import anthropic
import re

client = anthropic.Anthropic()

def extract_factual_claims(text: str) -> list[str]:
    """Extract sentences that make specific factual claims."""
    sentences = re.split(r'(?<=[.!?])\s+', text)
    factual_patterns = [
        r'\b(is|was|were|are|has|have|had|invented|created|founded|born|died)\b',
        r'\b\d{4}\b',   # years
        r'\b\d+\s*(million|billion|thousand|percent|%)\b',
    ]
    factual = []
    for s in sentences:
        if any(re.search(p, s, re.I) for p in factual_patterns):
            factual.append(s.strip())
    return factual[:5]

def verify_with_confidence(claim: str) -> tuple[str, float]:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content":
            f"Verify this claim and rate your confidence 0-10:\n\nClaim: {claim}\n\n"
            "Format:\nVERDICT: [correct/incorrect/uncertain]\n"
            "CORRECTION: [corrected fact if wrong, or 'none']\n"
            "CONFIDENCE: [0-10]"}],
    )
    text = resp.content[0].text
    verdict = "uncertain"
    correction = "none"
    confidence = 5.0

    for line in text.split("\n"):
        if line.startswith("VERDICT:"):
            verdict = line.split(":", 1)[1].strip().lower()
        elif line.startswith("CORRECTION:"):
            correction = line.split(":", 1)[1].strip()
        elif line.startswith("CONFIDENCE:"):
            try:
                confidence = float(re.search(r"\d+", line).group()) / 10.0
            except Exception:
                pass

    return (correction if verdict == "incorrect" and correction != "none" else claim), confidence

def cove_with_confidence(question: str, confidence_threshold: float = 0.7) -> str:
    # Step 1: Draft
    draft_resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": question}],
    )
    draft = draft_resp.content[0].text

    # Step 2: Extract and verify claims
    claims = extract_factual_claims(draft)
    verified_claims = []
    low_confidence_count = 0
    for claim in claims:
        corrected, conf = verify_with_confidence(claim)
        verified_claims.append(corrected)
        if conf < confidence_threshold:
            low_confidence_count += 1
        print(f"  [{conf:.1f}] {claim[:50]!r}")
        if corrected != claim:
            print(f"    -> corrected: {corrected[:60]}")

    # Step 3: Reconstruct answer
    context = (
        f"Question: {question}\nDraft:\n{draft}\n\n"
        f"Verified facts:\n" + "\n".join(f"- {c}" for c in verified_claims)
    )
    if low_confidence_count > 1:
        context += "\n\nNote: Some facts had low confidence. Be appropriately hedged."

    final_resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content":
            context + "\n\nWrite a corrected, accurate final answer."}],
    )
    return final_resp.content[0].text

result = cove_with_confidence("When was the internet invented and who invented it?")
print(f"\nFinal:\n{result}")

# Expected Token Savings: Confidence filtering skips full correction for high-confidence claims
# Environment: any; regex-based claim extraction works without NLP libraries
```

## Option 4: Iterative Verification with Self-Correction Loop

```python
import anthropic

client = anthropic.Anthropic()

def run_cove_loop(question: str, max_iterations: int = 3) -> str:
    """Iterate: generate, verify, correct — until no errors found or max iterations."""

    current_answer = ""
    for iteration in range(1, max_iterations + 1):
        print(f"\n--- Iteration {iteration} ---")

        # Generate or regenerate answer
        if iteration == 1:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": question}],
            )
        else:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content":
                    f"Question: {question}\n\nPrevious answer:\n{current_answer}\n\n"
                    f"Corrections from verification:\n{corrections_text}\n\n"
                    "Write an improved answer that fixes all identified errors."}],
            )
        current_answer = resp.content[0].text
        print(f"Answer: {current_answer[:100]}")

        # Self-verification
        ver_resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content":
                f"Question: {question}\nAnswer: {current_answer}\n\n"
                "Find any factual errors in this answer. "
                "If the answer is fully correct, reply with 'NO_ERRORS'. "
                "Otherwise list each error as: ERROR: [description] | CORRECTION: [fix]"}],
        )
        ver_text = ver_resp.content[0].text
        print(f"Verification: {ver_text[:120]}")

        if "NO_ERRORS" in ver_text.upper() or "no errors" in ver_text.lower():
            print("Verification passed! No further corrections needed.")
            break

        # Extract corrections for next iteration
        corrections = []
        for line in ver_text.split("\n"):
            if "ERROR:" in line:
                corrections.append(line.strip())
        if not corrections:
            break
        corrections_text = "\n".join(corrections)

    return current_answer

result = run_cove_loop(
    "What is the difference between TCP and UDP? Give specific examples of protocols that use each."
)
print(f"\nFinal verified answer:\n{result}")

# Expected Token Savings: Loop exits early on correct answers; 1-2 iterations typical
# Environment: any; max_iterations=3 prevents infinite loops; reduce to 2 for speed
```

## Option 5: Structured CoVe with Separate Verifier Model

```python
import anthropic

client = anthropic.Anthropic()

GENERATOR_MODEL = "claude-haiku-4-5-20251001"
VERIFIER_MODEL  = "claude-sonnet-4-6"      # Stronger model for verification

def generate(question: str) -> str:
    resp = client.messages.create(
        model=GENERATOR_MODEL,
        max_tokens=256,
        messages=[{"role": "user", "content": question}],
    )
    return resp.content[0].text

def extract_verification_plan(question: str, answer: str) -> list[dict]:
    resp = client.messages.create(
        model=GENERATOR_MODEL,
        max_tokens=256,
        messages=[{"role": "user", "content":
            f"Question: {question}\nAnswer:\n{answer}\n\n"
            "Create a verification plan. For each major claim, write:\n"
            "CLAIM: [the claim]\n"
            "VERIFY_Q: [question to independently verify this claim]\n"
            "---"}],
    )
    plan = []
    blocks = resp.content[0].text.split("---")
    for block in blocks:
        claim, verify_q = "", ""
        for line in block.split("\n"):
            if line.startswith("CLAIM:"):
                claim = line[6:].strip()
            elif line.startswith("VERIFY_Q:"):
                verify_q = line[9:].strip()
        if claim and verify_q:
            plan.append({"claim": claim, "verify_q": verify_q})
    return plan[:4]

def verify_with_strong_model(verify_q: str) -> str:
    """Use stronger model for independent verification."""
    resp = client.messages.create(
        model=VERIFIER_MODEL,
        max_tokens=128,
        messages=[{"role": "user", "content":
            f"Answer this precisely and accurately: {verify_q}"}],
    )
    return resp.content[0].text.strip()

def synthesize(question: str, original_answer: str,
               plan: list[dict], verifications: list[str]) -> str:
    context = "\n".join(
        f"Claim: {p['claim']}\nVerified fact: {v}"
        for p, v in zip(plan, verifications)
    )
    resp = client.messages.create(
        model=GENERATOR_MODEL,
        max_tokens=256,
        messages=[{"role": "user", "content":
            f"Question: {question}\n\nOriginal answer:\n{original_answer}\n\n"
            f"Verified facts:\n{context}\n\n"
            "Write a final, accurate answer based on the verified facts."}],
    )
    return resp.content[0].text

def cove_two_model(question: str) -> str:
    answer = generate(question)
    plan = extract_verification_plan(question, answer)
    if not plan:
        return answer
    print(f"Verification plan: {len(plan)} claims")
    verifications = [verify_with_strong_model(p["verify_q"]) for p in plan]
    for p, v in zip(plan, verifications):
        print(f"  Claim: {p['claim'][:50]}")
        print(f"  Fact:  {v[:60]}")
    return synthesize(question, answer, plan, verifications)

result = cove_two_model(
    "What are the key differences between PostgreSQL and MySQL? "
    "Include specific version-specific features."
)
print(f"\nFinal:\n{result}")

# Expected Token Savings: Haiku generates (cheap), Sonnet verifies (accurate); combined cost < Opus solo
# Environment: two-model pattern; swap VERIFIER_MODEL to Opus for highest accuracy requirements
```

## Option 6: CoVe with Factual Grounding from Tool Results

```python
import anthropic

client = anthropic.Anthropic()

# Simulated knowledge base / tool results
KNOWLEDGE_BASE = {
    "python": "Python was created by Guido van Rossum and first released in 1991. "
              "Python 2.0 released in 2000; Python 3.0 in 2008.",
    "linux":  "Linux kernel was created by Linus Torvalds, first released on September 17, 1991.",
    "git":    "Git was created by Linus Torvalds in 2005 for Linux kernel development.",
    "http":   "HTTP/1.0 published 1996 (RFC 1945); HTTP/1.1 in 1997; HTTP/2 in 2015; HTTP/3 in 2022.",
}

def retrieve_facts(query: str) -> str:
    """Retrieve relevant facts from knowledge base for grounding."""
    q = query.lower()
    hits = []
    for key, fact in KNOWLEDGE_BASE.items():
        if key in q or any(w in q for w in key.split()):
            hits.append(fact)
    return "\n".join(hits) if hits else ""

def cove_grounded(question: str) -> str:
    # Step 1: Draft
    draft_resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": question}],
    )
    draft = draft_resp.content[0].text
    print(f"Draft: {draft[:120]}")

    # Step 2: Retrieve grounding facts
    ground = retrieve_facts(question)
    print(f"Grounding: {ground[:120] if ground else '(none found)'}")

    # Step 3: Generate verification questions
    vq_resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content":
            f"Draft: {draft}\n\nList 3 specific claims to verify. One per line."}],
    )
    claims = [l.strip() for l in vq_resp.content[0].text.split("\n") if l.strip()][:3]

    # Step 4: Verify each claim against retrieved facts (or model knowledge)
    verifications = []
    for claim in claims:
        ctx = f"Known facts:\n{ground}\n\n" if ground else ""
        ver_resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=96,
            messages=[{"role": "user", "content":
                f"{ctx}Verify this claim. State if correct or provide correction:\n{claim}"}],
        )
        verifications.append((claim, ver_resp.content[0].text.strip()))

    # Step 5: Final answer with grounding
    ver_text = "\n".join(f"- {c}: {v[:80]}" for c, v in verifications)
    ground_ctx = f"\nVerified source facts:\n{ground}" if ground else ""
    final_resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content":
            f"Question: {question}\nDraft:\n{draft}\n\n"
            f"Verification results:\n{ver_text}{ground_ctx}\n\n"
            "Write the final accurate answer."}],
    )
    return final_resp.content[0].text

result = cove_grounded("When was Python created and what were the major version milestones?")
print(f"\nFinal:\n{result}")

result2 = cove_grounded("Who created Git and when?")
print(f"\nFinal:\n{result2}")

# Expected Token Savings: Grounded verification reduces verification errors; 5 Haiku calls vs 1 Opus
# Environment: swap KNOWLEDGE_BASE with vector DB / RAG retrieval for production
```

## Comparison

| Option | Verification Method | Parallelism | Model Strategy |
|--------|-------------------|-------------|---------------|
| 1 — Basic Three-Step | Sequential claim questions | None | Haiku throughout |
| 2 — Parallel async | Concurrent claim verification | Full parallel | Haiku throughout |
| 3 — Confidence Scoring | Per-claim confidence rating | None | Haiku + confidence filter |
| 4 — Iterative Loop | Self-correction until no errors | None | Haiku; exits early |
| 5 — Two-Model | Strong model verifies claims | None | Haiku generates, Sonnet verifies |
| 6 — Grounded CoVe | Tool/KB facts anchor verification | None | Haiku + knowledge base |
