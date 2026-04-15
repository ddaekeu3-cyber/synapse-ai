---
layout: solution
title: "Agent Doesn't Implement Retrieval-Augmented Verification"
category: hallucination
description: "Agents that answer from parametric memory alone cannot distinguish between what they know and what they fabricated. Retrieval-Augmented Verification (RAV) grounds every factual claim by retrieving supporting evidence and checking whether the claim is entailed, contradicted, or unverifiable from the retrieved sources."
tags: [hallucination, rav, retrieval, verification, grounding, fact-checking, rag, nli]
---

# Agent Doesn't Implement Retrieval-Augmented Verification

## Problem

Standard RAG retrieves documents to help the model answer, but doesn't check whether the answer it produced is actually supported by those documents. The model can ignore the retrieved context, blend it with hallucinated details, or produce claims that directly contradict the sources. Retrieval-Augmented Verification adds a second pass: after the model responds, each factual claim is matched to retrieved evidence and labeled entailed, contradicted, or unverifiable. Unverifiable or contradicted claims are either flagged or regenerated.

**Symptoms:**
- Model answers confidently with details not in retrieved documents
- Citations in responses don't actually support the claim they're attached to
- Factual errors mixed into otherwise well-grounded responses
- No way to distinguish high-confidence from hallucinated content
- Users can't tell which parts of the response are sourced vs invented

---

## Option 1: Claim Extraction + Evidence Matching

```python
import anthropic
import json
from dataclasses import dataclass, field
from typing import Literal

VerificationStatus = Literal["entailed", "contradicted", "unverifiable"]

@dataclass
class Claim:
    text: str
    status: VerificationStatus = "unverifiable"
    supporting_evidence: str = ""
    confidence: float = 0.0

@dataclass
class VerifiedResponse:
    original_response: str
    claims: list[Claim]
    verified_count: int = 0
    contradicted_count: int = 0
    unverifiable_count: int = 0

    def trust_score(self) -> float:
        if not self.claims:
            return 0.0
        return self.verified_count / len(self.claims)

def extract_claims(client: anthropic.Anthropic, response_text: str) -> list[str]:
    """Extract atomic factual claims from a response."""
    extraction = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system="""Extract all atomic factual claims from the text.
Return a JSON array of strings. Each string is one specific, verifiable fact.
Ignore opinions, hedges, and questions. Example:
["Python was released in 1991", "Python is dynamically typed"]""",
        messages=[{"role": "user", "content": f"Extract claims from:\n{response_text}"}]
    )
    text = extraction.content[0].text.strip()
    try:
        start = text.find("[")
        end = text.rfind("]") + 1
        return json.loads(text[start:end])
    except (json.JSONDecodeError, ValueError):
        return [response_text[:200]]

def verify_claim_against_evidence(
    client: anthropic.Anthropic,
    claim: str,
    evidence_passages: list[str]
) -> tuple[VerificationStatus, str, float]:
    """Check if a claim is entailed, contradicted, or unverifiable given evidence."""
    evidence_text = "\n---\n".join(f"[{i+1}] {p}" for i, p in enumerate(evidence_passages))

    verification = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system="""You are a fact-checker. Given evidence passages and a claim, determine:
- "entailed": the evidence clearly supports the claim
- "contradicted": the evidence contradicts the claim
- "unverifiable": the evidence doesn't contain enough information to verify

Respond with JSON: {"status": "entailed|contradicted|unverifiable", "evidence_ref": "quote the supporting/contradicting phrase", "confidence": 0.0-1.0}""",
        messages=[{
            "role": "user",
            "content": f"Evidence:\n{evidence_text}\n\nClaim: {claim}"
        }]
    )
    text = verification.content[0].text.strip()
    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        data = json.loads(text[start:end])
        return data["status"], data.get("evidence_ref", ""), data.get("confidence", 0.5)
    except (json.JSONDecodeError, ValueError, KeyError):
        return "unverifiable", "", 0.0

def retrieval_augmented_verify(
    client: anthropic.Anthropic,
    query: str,
    response: str,
    retrieved_passages: list[str]
) -> VerifiedResponse:
    claims_text = extract_claims(client, response)
    claims = [Claim(text=c) for c in claims_text]

    print(f"\nVerifying {len(claims)} claims against {len(retrieved_passages)} passages:")
    for claim in claims:
        status, evidence, confidence = verify_claim_against_evidence(
            client, claim.text, retrieved_passages
        )
        claim.status = status
        claim.supporting_evidence = evidence
        claim.confidence = confidence

        icon = {"entailed": "✓", "contradicted": "✗", "unverifiable": "?"}[status]
        print(f"  [{icon}] {claim.text[:70]!r} ({confidence:.0%})")

    result = VerifiedResponse(
        original_response=response,
        claims=claims,
        verified_count=sum(1 for c in claims if c.status == "entailed"),
        contradicted_count=sum(1 for c in claims if c.status == "contradicted"),
        unverifiable_count=sum(1 for c in claims if c.status == "unverifiable")
    )
    return result

# Simulate retrieved passages (in production: from vector DB or search)
passages = [
    "Python is a high-level, general-purpose programming language created by Guido van Rossum. The first version was released in 1991.",
    "Python uses dynamic typing and automatic memory management via garbage collection.",
    "Python 3.0 was released in 2008 and introduced breaking changes from Python 2.",
    "Python is widely used in data science, machine learning, web development, and automation.",
]

client = anthropic.Anthropic()
query = "Tell me about Python programming language"

# Generate a response (may contain hallucinations)
response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=300,
    messages=[{
        "role": "user",
        "content": f"Context:\n{''.join(passages)}\n\nQuestion: {query}"
    }]
)
response_text = response.content[0].text

print(f"Response:\n{response_text}\n")
result = retrieval_augmented_verify(client, query, response_text, passages)

print(f"\nVerification Summary:")
print(f"  Trust score: {result.trust_score():.0%}")
print(f"  Entailed: {result.verified_count}/{len(result.claims)}")
print(f"  Contradicted: {result.contradicted_count}")
print(f"  Unverifiable: {result.unverifiable_count}")

# Expected Token Savings: -200% (verification doubles token usage) — reduces hallucination risk
# Environment: Best for high-stakes domains: medical, legal, financial, technical documentation
```

---

## Option 2: Citation Grounding — Force Every Claim to Cite Its Source

```python
import anthropic
import json
import re
from dataclasses import dataclass, field

@dataclass
class CitedClaim:
    claim: str
    citation_ids: list[int]
    verified: bool = False
    grounding_text: str = ""

def generate_cited_response(
    client: anthropic.Anthropic,
    query: str,
    passages: list[dict]  # [{"id": 1, "text": "...", "source": "..."}]
) -> tuple[str, list[CitedClaim]]:
    """Ask the model to generate a response with inline citations."""
    numbered_passages = "\n".join(
        f"[{p['id']}] {p['text']}" for p in passages
    )

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        system="""Answer using ONLY the provided passages.
After each factual claim, add a citation like [1] or [1,3].
If a claim cannot be supported by any passage, write (unsupported).
Never state facts not present in the passages.""",
        messages=[{
            "role": "user",
            "content": f"Passages:\n{numbered_passages}\n\nQuestion: {query}"
        }]
    )
    text = response.content[0].text

    # Extract cited claims
    claims = []
    sentences = re.split(r"(?<=[.!?])\s+", text)
    for sentence in sentences:
        citation_match = re.findall(r"\[(\d+(?:,\d+)*)\]", sentence)
        cited_ids = []
        for match in citation_match:
            cited_ids.extend(int(x) for x in match.split(","))
        claims.append(CitedClaim(claim=sentence, citation_ids=cited_ids))

    return text, claims

def verify_citations(
    client: anthropic.Anthropic,
    claims: list[CitedClaim],
    passages: list[dict]
) -> list[CitedClaim]:
    """Verify that each cited claim is actually supported by its cited passages."""
    passage_map = {p["id"]: p["text"] for p in passages}

    for claim in claims:
        if not claim.citation_ids:
            if "(unsupported)" in claim.claim:
                claim.verified = False
            continue

        # Get cited passage text
        cited_texts = [passage_map.get(pid, "") for pid in claim.citation_ids if pid in passage_map]
        if not cited_texts:
            claim.verified = False
            continue

        # Verify grounding
        check = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            system="Does the claim follow from the evidence? Reply YES or NO and one line why.",
            messages=[{
                "role": "user",
                "content": f"Evidence: {' | '.join(cited_texts)}\nClaim: {claim.claim}"
            }]
        )
        verdict = check.content[0].text.strip()
        claim.verified = verdict.upper().startswith("YES")
        claim.grounding_text = verdict[:80]

    return claims

passages = [
    {"id": 1, "text": "The Python language was created by Guido van Rossum and first released in 1991.", "source": "wiki"},
    {"id": 2, "text": "Python supports multiple programming paradigms including procedural, object-oriented, and functional programming.", "source": "docs"},
    {"id": 3, "text": "Python's package manager pip allows installing third-party libraries from PyPI.", "source": "docs"},
    {"id": 4, "text": "Python is used extensively in data science libraries such as NumPy, Pandas, and scikit-learn.", "source": "survey"},
]

client = anthropic.Anthropic()
query = "What should I know about Python as a new programmer?"

cited_response, claims = generate_cited_response(client, query, passages)
print(f"Cited response:\n{cited_response}\n")

verified_claims = verify_citations(client, claims, passages)
print("Citation verification:")
for claim in verified_claims:
    if claim.citation_ids:
        status = "✓ verified" if claim.verified else "✗ unsupported"
        print(f"  [{','.join(str(i) for i in claim.citation_ids)}] {status}: {claim.claim[:60]}")

uncited_count = sum(1 for c in verified_claims if not c.citation_ids)
failed_count = sum(1 for c in verified_claims if c.citation_ids and not c.verified)
print(f"\nSummary: {failed_count} failed citations, {uncited_count} uncited claims")

# Expected Token Savings: ~0% — citation generation + verification doubles calls; accuracy worth it
# Environment: Document Q&A, legal review, research assistants requiring audit trail
```

---

## Option 3: Entailment Scoring — Rewrite Low-Confidence Claims

```python
import anthropic
import json
from dataclasses import dataclass

@dataclass
class ScoredClaim:
    original: str
    entailment_score: float  # 0.0 = contradiction, 0.5 = neutral, 1.0 = fully entailed
    rewritten: str = ""
    was_rewritten: bool = False

def score_entailment(
    client: anthropic.Anthropic,
    claim: str,
    evidence: list[str]
) -> float:
    """Score how well a claim is entailed by the evidence (0.0–1.0)."""
    evidence_block = "\n".join(f"- {e}" for e in evidence)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        system='Score claim entailment from 0.0 to 1.0. Respond with JSON: {"score": 0.75, "reason": "..."}',
        messages=[{"role": "user", "content": f"Evidence:\n{evidence_block}\n\nClaim: {claim}"}]
    )
    text = resp.content[0].text.strip()
    try:
        start, end = text.find("{"), text.rfind("}") + 1
        return float(json.loads(text[start:end]).get("score", 0.5))
    except Exception:
        return 0.5

def rewrite_grounded(
    client: anthropic.Anthropic,
    claim: str,
    evidence: list[str]
) -> str:
    """Rewrite the claim to only state what the evidence supports."""
    evidence_block = "\n".join(f"- {e}" for e in evidence)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system="Rewrite the claim using ONLY information in the evidence. If nothing supports it, respond: [cannot verify]",
        messages=[{"role": "user", "content": f"Evidence:\n{evidence_block}\n\nOriginal claim: {claim}"}]
    )
    return resp.content[0].text.strip()

def entailment_verified_response(
    client: anthropic.Anthropic,
    query: str,
    evidence_passages: list[str],
    rewrite_threshold: float = 0.5
) -> tuple[str, list[ScoredClaim]]:
    # Step 1: Generate initial response
    context = "\n".join(f"[Context]: {p}" for p in evidence_passages)
    initial = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": f"{context}\n\nQuestion: {query}"}]
    )
    initial_text = initial.content[0].text

    # Step 2: Split into sentences and score each
    import re
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", initial_text) if len(s.strip()) > 15]
    scored = []

    print(f"\nScoring {len(sentences)} claims:")
    for sentence in sentences:
        score = score_entailment(client, sentence, evidence_passages)
        claimed = ScoredClaim(original=sentence, entailment_score=score)
        print(f"  [{score:.2f}] {sentence[:70]!r}")

        if score < rewrite_threshold:
            claimed.rewritten = rewrite_grounded(client, sentence, evidence_passages)
            claimed.was_rewritten = True
            print(f"    -> REWRITTEN: {claimed.rewritten[:70]!r}")

        scored.append(claimed)

    # Step 3: Compose verified response
    final_sentences = [
        (c.rewritten if c.was_rewritten else c.original)
        for c in scored
        if not (c.was_rewritten and "[cannot verify]" in c.rewritten)
    ]
    verified_response = " ".join(final_sentences)

    return verified_response, scored

evidence = [
    "The Great Wall of China was built over many centuries, starting in the 7th century BC.",
    "The wall stretches approximately 21,196 kilometers (13,171 miles) in total length.",
    "The Ming dynasty (1368–1644) built the most well-known sections of the wall.",
    "The wall was primarily built to protect against nomadic invasions from the north.",
]

client = anthropic.Anthropic()
response, scored_claims = entailment_verified_response(
    client,
    query="Tell me about the Great Wall of China",
    evidence_passages=evidence,
    rewrite_threshold=0.6
)

rewritten = sum(1 for c in scored_claims if c.was_rewritten)
print(f"\nFinal verified response:\n{response}")
print(f"\n{rewritten}/{len(scored_claims)} claims were rewritten for grounding")

# Expected Token Savings: -150% overhead — but eliminates hallucinated claims in high-stakes responses
# Environment: Medical, legal, financial agents where factual accuracy is non-negotiable
```

---

## Option 4: Multi-Source Consensus Verification

```python
import anthropic
import json
from dataclasses import dataclass, field
from collections import Counter

@dataclass
class SourceVerification:
    source_id: str
    source_text: str
    verdict: str  # "supports", "contradicts", "neutral"
    quote: str = ""

@dataclass
class ConsensusResult:
    claim: str
    source_verdicts: list[SourceVerification]
    consensus: str  # "verified", "disputed", "unverifiable"
    support_count: int = 0
    contradict_count: int = 0

    def confidence(self) -> float:
        total = len(self.source_verdicts)
        if total == 0:
            return 0.0
        return self.support_count / total

def verify_against_source(
    client: anthropic.Anthropic,
    claim: str,
    source_id: str,
    source_text: str
) -> SourceVerification:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system='Does the source support, contradict, or is neutral to the claim? JSON: {"verdict": "supports|contradicts|neutral", "quote": "relevant phrase from source"}',
        messages=[{"role": "user", "content": f"Source: {source_text}\n\nClaim: {claim}"}]
    )
    text = resp.content[0].text.strip()
    try:
        s, e = text.find("{"), text.rfind("}") + 1
        data = json.loads(text[s:e])
        return SourceVerification(
            source_id=source_id,
            source_text=source_text,
            verdict=data.get("verdict", "neutral"),
            quote=data.get("quote", "")
        )
    except Exception:
        return SourceVerification(source_id=source_id, source_text=source_text, verdict="neutral")

def multi_source_consensus(
    client: anthropic.Anthropic,
    claim: str,
    sources: dict[str, str],
    min_sources_for_verification: int = 2
) -> ConsensusResult:
    verdicts = []
    for sid, stext in sources.items():
        v = verify_against_source(client, claim, sid, stext)
        verdicts.append(v)

    support = sum(1 for v in verdicts if v.verdict == "supports")
    contradict = sum(1 for v in verdicts if v.verdict == "contradicts")

    if support >= min_sources_for_verification and contradict == 0:
        consensus = "verified"
    elif contradict > support:
        consensus = "disputed"
    else:
        consensus = "unverifiable"

    return ConsensusResult(
        claim=claim,
        source_verdicts=verdicts,
        consensus=consensus,
        support_count=support,
        contradict_count=contradict
    )

def run_multi_source_verification(query: str, sources: dict[str, str]):
    client = anthropic.Anthropic()

    # Generate response from all sources combined
    context = "\n\n".join(f"[{sid}]: {text}" for sid, text in sources.items())
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=350,
        messages=[{"role": "user", "content": f"{context}\n\nQuestion: {query}"}]
    )
    response_text = response.content[0].text

    # Extract key claims and verify against all sources
    claims_resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system='Extract up to 4 key factual claims. Return JSON array: ["claim1", "claim2"]',
        messages=[{"role": "user", "content": response_text}]
    )
    try:
        ctext = claims_resp.content[0].text
        s, e = ctext.find("["), ctext.rfind("]") + 1
        claims = json.loads(ctext[s:e])
    except Exception:
        claims = [response_text[:100]]

    print(f"Response:\n{response_text}\n")
    print("Multi-source verification:")
    results = []
    for claim in claims[:4]:
        result = multi_source_consensus(client, claim, sources)
        icon = {"verified": "✓", "disputed": "✗", "unverifiable": "?"}[result.consensus]
        print(f"  [{icon}] {claim[:60]!r}")
        print(f"      supports={result.support_count}, contradicts={result.contradict_count}, "
              f"confidence={result.confidence():.0%}")
        results.append(result)

    verified = sum(1 for r in results if r.consensus == "verified")
    disputed = sum(1 for r in results if r.consensus == "disputed")
    print(f"\nOverall: {verified}/{len(results)} verified, {disputed} disputed")
    return results

sources = {
    "source_a": "The speed of light in a vacuum is approximately 299,792,458 meters per second. This is a fundamental physical constant.",
    "source_b": "Light travels at roughly 3×10^8 m/s in vacuum. In other media like glass, it travels slower.",
    "source_c": "Einstein's special relativity establishes that the speed of light (c) is constant in all inertial reference frames.",
}

run_multi_source_verification(
    query="What is the speed of light and why does it matter?",
    sources=sources
)

# Expected Token Savings: -300% — cross-source verification is expensive; use for high-stakes facts only
# Environment: Research assistants, medical QA, financial analysis with multiple authoritative sources
```

---

## Option 5: Streaming Verification with Real-Time Flagging

```python
import anthropic
import re
import time
from dataclasses import dataclass, field

@dataclass
class StreamingVerificationResult:
    sentence: str
    is_verifiable: bool
    confidence: float
    flag: str  # "", "LOW_CONFIDENCE", "CONTRADICTED"

def verify_sentence_fast(
    client: anthropic.Anthropic,
    sentence: str,
    evidence: list[str]
) -> StreamingVerificationResult:
    """Quick verification for a single sentence during streaming."""
    if len(sentence.split()) < 5:
        return StreamingVerificationResult(sentence, False, 1.0, "")

    evidence_short = " | ".join(e[:100] for e in evidence[:3])

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=48,
        system='Quick fact check. Reply: SUPPORTED 0.9 or UNSUPPORTED 0.3 or NEUTRAL 0.5 (just these two tokens)',
        messages=[{"role": "user", "content": f"Evidence: {evidence_short}\nClaim: {sentence}"}]
    )
    text = resp.content[0].text.strip().upper()
    if text.startswith("SUPPORTED"):
        try:
            conf = float(text.split()[1])
        except (IndexError, ValueError):
            conf = 0.8
        return StreamingVerificationResult(sentence, True, conf, "")
    elif text.startswith("UNSUPPORTED"):
        try:
            conf = float(text.split()[1])
        except (IndexError, ValueError):
            conf = 0.3
        flag = "LOW_CONFIDENCE" if conf > 0.2 else "CONTRADICTED"
        return StreamingVerificationResult(sentence, True, conf, flag)
    return StreamingVerificationResult(sentence, False, 0.5, "")

def stream_with_verification(
    client: anthropic.Anthropic,
    query: str,
    evidence: list[str]
):
    """Stream a response and verify each sentence as it's completed."""
    context = "\n".join(f"[Doc]: {e}" for e in evidence)

    print("Generating and verifying response in real-time:\n")
    buffer = ""
    flagged_sentences = []

    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        messages=[{"role": "user", "content": f"{context}\n\nQuestion: {query}"}]
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
            buffer += text

            # Check for sentence boundaries
            while True:
                match = re.search(r"(?<=[.!?])\s+", buffer)
                if not match:
                    break
                sentence = buffer[:match.start() + 1].strip()
                buffer = buffer[match.end():]

                if len(sentence.split()) >= 5:
                    result = verify_sentence_fast(client, sentence, evidence)
                    if result.flag:
                        flagged_sentences.append(result)
                        print(f" ⚠️", end="", flush=True)

    # Verify remaining buffer
    if buffer.strip() and len(buffer.split()) >= 5:
        result = verify_sentence_fast(client, buffer.strip(), evidence)
        if result.flag:
            flagged_sentences.append(result)

    print(f"\n\n{'='*50}")
    print(f"Verification summary: {len(flagged_sentences)} flagged sentences")
    for r in flagged_sentences:
        print(f"  [{r.flag}] ({r.confidence:.0%}): {r.sentence[:70]!r}")

evidence = [
    "Python was created by Guido van Rossum and first released in 1991.",
    "Python 3 introduced several breaking changes from Python 2 in 2008.",
    "Python is consistently ranked as one of the most popular programming languages.",
]

client = anthropic.Anthropic()
stream_with_verification(
    client,
    query="Give me a brief history of Python and its key features",
    evidence=evidence
)

# Expected Token Savings: ~0% — verification calls match output tokens; value is real-time UX feedback
# Environment: Chat interfaces where users see responses incrementally; flag suspicious claims inline
```

---

## Option 6: Full RAV Pipeline with Regeneration

```python
import anthropic
import json
import re
from dataclasses import dataclass, field

@dataclass
class RAVConfig:
    min_trust_score: float = 0.7        # Regenerate if below this
    max_regeneration_attempts: int = 2  # Try at most N times
    rewrite_contradicted: bool = True   # Rewrite contradicted claims
    drop_unverifiable: bool = False     # Drop or keep unverifiable claims

@dataclass
class RAVPipelineResult:
    final_response: str
    trust_score: float
    attempts: int
    claims_total: int
    claims_verified: int
    claims_contradicted: int
    claims_unverifiable: int

def run_rav_pipeline(
    client: anthropic.Anthropic,
    query: str,
    evidence_passages: list[str],
    config: RAVConfig = None
) -> RAVPipelineResult:
    config = config or RAVConfig()
    context = "\n".join(f"[Evidence {i+1}]: {p}" for i, p in enumerate(evidence_passages))

    for attempt in range(config.max_regeneration_attempts + 1):
        # Step 1: Generate response
        system = "Answer using ONLY the provided evidence. Be specific and cite the relevant facts."
        if attempt > 0:
            system += " Previous attempt had unverified claims. Be more conservative."

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=system,
            messages=[{"role": "user", "content": f"{context}\n\nQuestion: {query}"}]
        )
        response_text = response.content[0].text

        # Step 2: Extract claims
        claims_resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system='List factual claims. Return JSON: [{"claim": "...", "type": "factual|opinion"}]',
            messages=[{"role": "user", "content": response_text}]
        )
        try:
            ct = claims_resp.content[0].text
            s, e = ct.find("["), ct.rfind("]") + 1
            claims_data = json.loads(ct[s:e])
            factual_claims = [c["claim"] for c in claims_data if c.get("type") == "factual"]
        except Exception:
            factual_claims = re.split(r"(?<=[.!?])\s+", response_text)[:5]

        if not factual_claims:
            break

        # Step 3: Verify each claim
        verified = contradicted = unverifiable = 0
        contradicted_claims = []

        for claim in factual_claims:
            evidence_block = "\n".join(f"[{i+1}] {p}" for i, p in enumerate(evidence_passages))
            check = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                system='Return JSON: {"status": "entailed|contradicted|unverifiable"}',
                messages=[{"role": "user", "content": f"Evidence:\n{evidence_block}\n\nClaim: {claim}"}]
            )
            try:
                ct = check.content[0].text
                s, e = ct.find("{"), ct.rfind("}") + 1
                status = json.loads(ct[s:e]).get("status", "unverifiable")
            except Exception:
                status = "unverifiable"

            if status == "entailed":
                verified += 1
            elif status == "contradicted":
                contradicted += 1
                contradicted_claims.append(claim)
            else:
                unverifiable += 1

        total = len(factual_claims)
        trust_score = verified / total if total else 0.0

        print(f"  Attempt {attempt+1}: trust={trust_score:.0%} "
              f"(verified={verified}, contradicted={contradicted}, unverifiable={unverifiable})")

        # Step 4: Accept or regenerate
        if trust_score >= config.min_trust_score and contradicted == 0:
            return RAVPipelineResult(
                final_response=response_text,
                trust_score=trust_score,
                attempts=attempt + 1,
                claims_total=total,
                claims_verified=verified,
                claims_contradicted=contradicted,
                claims_unverifiable=unverifiable
            )

        if attempt == config.max_regeneration_attempts:
            # Last attempt — return best we have
            return RAVPipelineResult(
                final_response=f"[Trust score: {trust_score:.0%}]\n{response_text}",
                trust_score=trust_score,
                attempts=attempt + 1,
                claims_total=total,
                claims_verified=verified,
                claims_contradicted=contradicted,
                claims_unverifiable=unverifiable
            )

    return RAVPipelineResult("", 0.0, config.max_regeneration_attempts + 1, 0, 0, 0, 0)

evidence = [
    "Mount Everest stands at 8,848.86 meters (29,031.7 feet) above sea level, confirmed by a 2020 survey.",
    "Edmund Hillary and Tenzing Norgay were the first climbers confirmed to have reached the summit on May 29, 1953.",
    "Everest is located in the Mahalangur Himal sub-range of the Himalayas on the border between Nepal and Tibet.",
    "More than 300 people have died attempting to climb Everest; the 'Death Zone' above 8,000m is the most dangerous.",
]

client = anthropic.Anthropic()
config = RAVConfig(min_trust_score=0.75, max_regeneration_attempts=2)

print("Running RAV Pipeline:\n")
result = run_rav_pipeline(client, "What should I know about climbing Mount Everest?", evidence, config)

print(f"\nFinal response (trust={result.trust_score:.0%}, attempts={result.attempts}):")
print(result.final_response)
print(f"\nClaims: {result.claims_verified} verified / {result.claims_total} total, "
      f"{result.claims_contradicted} contradicted")

# Expected Token Savings: -300% to -500% — regeneration multiplies cost; gate to high-stakes queries only
# Environment: Medical advice, legal research, financial analysis — where correctness > token cost
```

---

## Comparison

| Option | Approach | Accuracy | Cost Overhead | Best For |
|--------|----------|----------|---------------|----------|
| Claim Extraction + Match | Atomic claim NLI | High | 2x | General-purpose fact verification |
| Citation Grounding | Forced inline citations | High | 2x | Document Q&A with audit trail |
| Entailment Scoring + Rewrite | Score + rewrite low-conf | Very High | 3x | High-stakes responses requiring accuracy |
| Multi-Source Consensus | Cross-source agreement | Very High | 4x | Multiple authoritative sources available |
| Streaming Verification | Per-sentence real-time | Medium | 1.5x | Chat UIs with inline flagging |
| Full RAV Pipeline | Generate → verify → regenerate | Highest | 5x | Mission-critical accuracy (medical/legal) |

**Recommendation:** Start with **Option 1** (claim extraction + evidence matching) as the baseline RAV implementation. For production chat applications, use **Option 5** (streaming verification) to give users real-time confidence signals. Reserve the **full pipeline (Option 6)** for mission-critical domains where a wrong answer causes real harm.
