---
title: "Agent Doesn't Implement Claim Decomposition for Complex Assertions"
description: "How to break complex multi-part claims into atomic sub-claims and verify each independently, preventing composite hallucinations that contain a mix of true and false assertions."
categories: [hallucination]
difficulty: intermediate
---

Complex assertions like "Python was created in 1991 by Guido van Rossum and is now maintained by the PSF with over 500 contributors" contain multiple verifiable claims. Treating the composite as a single unit misses individual errors. Decomposing it into atomic claims and verifying each independently catches the false parts while preserving the true ones.

## Solution 1: LLM-Based Claim Decomposer

Use a fast model to decompose a complex assertion into atomic, independently verifiable sub-claims.

```python
import asyncio
import json
from dataclasses import dataclass, field
import anthropic

client = anthropic.AsyncAnthropic()
DECOMPOSER_MODEL = "claude-haiku-4-5-20251001"
VERIFIER_MODEL = "claude-sonnet-4-6"


@dataclass
class AtomicClaim:
    claim: str
    verifiable: bool
    verification_status: str = "pending"  # pending | supported | unsupported | uncertain
    confidence: float = 0.0
    evidence: str = ""


async def decompose_assertion(assertion: str) -> list[AtomicClaim]:
    resp = await client.messages.create(
        model=DECOMPOSER_MODEL,
        max_tokens=400,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Break this assertion into atomic, independently verifiable claims. "
                    f"Each claim should be a single checkable fact.\n\n"
                    f"Assertion: {assertion}\n\n"
                    f"Return JSON array: ["
                    f'  {{"claim": str, "verifiable": bool}}'
                    f"]"
                ),
            }
        ],
    )
    try:
        import re
        match = re.search(r"\[[\s\S]+\]", resp.content[0].text)
        raw = json.loads(match.group(0) if match else resp.content[0].text)
        return [AtomicClaim(claim=r["claim"], verifiable=r.get("verifiable", True)) for r in raw]
    except Exception:
        return [AtomicClaim(claim=assertion, verifiable=True)]


async def verify_claim(claim: AtomicClaim) -> AtomicClaim:
    if not claim.verifiable:
        claim.verification_status = "uncertain"
        claim.confidence = 0.5
        return claim

    resp = await client.messages.create(
        model=VERIFIER_MODEL,
        max_tokens=200,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Verify this claim based on your knowledge:\n{claim.claim}\n\n"
                    f"Reply with JSON: "
                    f'{{"status": "supported|unsupported|uncertain", "confidence": 0-1, "evidence": str}}'
                ),
            }
        ],
    )
    try:
        import re
        match = re.search(r"\{[\s\S]+\}", resp.content[0].text)
        data = json.loads(match.group(0) if match else resp.content[0].text)
        claim.verification_status = data.get("status", "uncertain")
        claim.confidence = float(data.get("confidence", 0.5))
        claim.evidence = data.get("evidence", "")
    except Exception:
        claim.verification_status = "uncertain"
        claim.confidence = 0.5

    return claim


async def decompose_and_verify(assertion: str) -> dict:
    claims = await decompose_assertion(assertion)
    verified = await asyncio.gather(*[verify_claim(c) for c in claims])

    supported = [c for c in verified if c.verification_status == "supported"]
    unsupported = [c for c in verified if c.verification_status == "unsupported"]
    uncertain = [c for c in verified if c.verification_status == "uncertain"]

    return {
        "assertion": assertion,
        "total_claims": len(verified),
        "supported": len(supported),
        "unsupported": len(unsupported),
        "uncertain": len(uncertain),
        "overall_reliability": len(supported) / len(verified) if verified else 0,
        "problems": [{"claim": c.claim, "evidence": c.evidence} for c in unsupported],
    }


async def main():
    assertions = [
        "Python was created in 1991 by Guido van Rossum and is maintained by the PSF.",
        "The Eiffel Tower is 324 meters tall and was built in 1887 in Paris.",
    ]

    results = await asyncio.gather(*[decompose_and_verify(a) for a in assertions])
    for r in results:
        print(f"\nAssertion: {r['assertion'][:80]}")
        print(f"  Claims: {r['total_claims']} | Supported: {r['supported']} | Unsupported: {r['unsupported']}")
        print(f"  Reliability: {r['overall_reliability']:.0%}")
        for p in r["problems"]:
            print(f"  [PROBLEM] {p['claim']}: {p['evidence'][:100]}")


asyncio.run(main())
```

## Solution 2: Dependency-Aware Claim Graph

Build a claim dependency graph where some claims depend on others, and propagate uncertainty through the graph.

```python
import asyncio
import json
from dataclasses import dataclass, field
from enum import Enum
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-haiku-4-5-20251001"


class ClaimStatus(Enum):
    PENDING = "pending"
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNCERTAIN = "uncertain"
    INVALIDATED = "invalidated"  # A dependency was unsupported


@dataclass
class ClaimNode:
    claim_id: str
    claim: str
    depends_on: list[str] = field(default_factory=list)  # IDs of prerequisite claims
    status: ClaimStatus = ClaimStatus.PENDING
    confidence: float = 0.5


async def build_claim_graph(assertion: str) -> dict[str, ClaimNode]:
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=500,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Decompose into claims with dependencies:\n{assertion}\n\n"
                    f"Return JSON array (claims that require others to be true first "
                    f"should list their depends_on):\n"
                    f'[{{"id": str, "claim": str, "depends_on": [str]}}]'
                ),
            }
        ],
    )
    try:
        import re
        match = re.search(r"\[[\s\S]+\]", resp.content[0].text)
        raw = json.loads(match.group(0) if match else resp.content[0].text)
        return {
            r["id"]: ClaimNode(
                claim_id=r["id"],
                claim=r["claim"],
                depends_on=r.get("depends_on", []),
            )
            for r in raw
        }
    except Exception:
        node = ClaimNode(claim_id="c1", claim=assertion)
        return {"c1": node}


async def verify_node(node: ClaimNode, graph: dict[str, ClaimNode]) -> None:
    # Check if any dependency is unsupported
    for dep_id in node.depends_on:
        dep = graph.get(dep_id)
        if dep and dep.status in (ClaimStatus.UNSUPPORTED, ClaimStatus.INVALIDATED):
            node.status = ClaimStatus.INVALIDATED
            node.confidence = 0.0
            return

    resp = await client.messages.create(
        model=MODEL,
        max_tokens=100,
        messages=[
            {
                "role": "user",
                "content": f"Is this claim factually accurate? '{node.claim}'\nReply: supported/unsupported/uncertain | confidence 0-1",
            }
        ],
    )
    text = resp.content[0].text.lower()
    if "unsupported" in text or "false" in text or "incorrect" in text:
        node.status = ClaimStatus.UNSUPPORTED
        node.confidence = 0.1
    elif "uncertain" in text:
        node.status = ClaimStatus.UNCERTAIN
        node.confidence = 0.5
    else:
        node.status = ClaimStatus.SUPPORTED
        node.confidence = 0.9


async def verify_graph(assertion: str) -> dict:
    graph = await build_claim_graph(assertion)

    # Topological order: verify nodes with no pending dependencies first
    verified: set[str] = set()
    for _ in range(len(graph) + 1):
        for node_id, node in graph.items():
            if node_id in verified:
                continue
            deps_ready = all(d in verified for d in node.depends_on)
            if deps_ready:
                await verify_node(node, graph)
                verified.add(node_id)

    problems = [n for n in graph.values() if n.status in (ClaimStatus.UNSUPPORTED, ClaimStatus.INVALIDATED)]
    return {
        "total": len(graph),
        "supported": sum(1 for n in graph.values() if n.status == ClaimStatus.SUPPORTED),
        "problems": [{"claim": n.claim, "status": n.status.value} for n in problems],
    }


async def main():
    result = await verify_graph(
        "Albert Einstein developed the theory of relativity in 1915, "
        "which led to him winning the Nobel Prize in Physics for that work."
    )
    print(json.dumps(result, indent=2))


asyncio.run(main())
```

## Solution 3: Claim Confidence Aggregator

Aggregate confidence scores from multiple atomic claims into an overall assertion confidence score using weighted combination.

```python
import asyncio
import json
import statistics
from dataclasses import dataclass, field
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-haiku-4-5-20251001"


@dataclass
class ScoredClaim:
    claim: str
    weight: float = 1.0  # Importance weight (1=normal, 2=critical)
    confidence: float = 0.5


async def score_claims_batch(claims: list[str]) -> list[ScoredClaim]:
    """Score a batch of claims in one API call."""
    claims_json = json.dumps([{"id": i, "claim": c} for i, c in enumerate(claims)])
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=400,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Score each claim for factual accuracy (confidence 0-1):\n{claims_json}\n\n"
                    f"Return JSON array: [{{\"id\": int, \"confidence\": float}}]"
                ),
            }
        ],
    )
    try:
        import re
        match = re.search(r"\[[\s\S]+\]", resp.content[0].text)
        scores = json.loads(match.group(0) if match else resp.content[0].text)
        score_map = {s["id"]: float(s["confidence"]) for s in scores}
        return [
            ScoredClaim(claim=c, confidence=score_map.get(i, 0.5))
            for i, c in enumerate(claims)
        ]
    except Exception:
        return [ScoredClaim(claim=c, confidence=0.5) for c in claims]


def aggregate_confidence(scored: list[ScoredClaim]) -> float:
    """Weighted harmonic mean — pulls toward the weakest claims."""
    if not scored:
        return 0.0
    total_weight = sum(s.weight for s in scored)
    # Weighted geometric mean (penalizes any single low-confidence claim)
    import math
    log_sum = sum(s.weight * math.log(max(s.confidence, 0.01)) for s in scored)
    return math.exp(log_sum / total_weight)


async def assess_composite_assertion(assertion: str) -> dict:
    # Step 1: Decompose
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=300,
        messages=[
            {
                "role": "user",
                "content": f"List atomic facts in this assertion, one per line:\n{assertion}"
            }
        ],
    )
    claims = [l.strip() for l in resp.content[0].text.splitlines() if l.strip()]

    # Step 2: Score in batch
    scored = await score_claims_batch(claims)

    # Step 3: Aggregate
    overall = aggregate_confidence(scored)
    low_confidence = [s for s in scored if s.confidence < 0.6]

    return {
        "assertion": assertion[:100],
        "claim_count": len(scored),
        "overall_confidence": round(overall, 3),
        "verdict": "reliable" if overall >= 0.8 else "uncertain" if overall >= 0.5 else "unreliable",
        "weak_claims": [{"claim": s.claim, "confidence": s.confidence} for s in low_confidence],
    }


async def main():
    assertions = [
        "The speed of light is approximately 300,000 km/s and was first measured by Ole Rømer in 1676.",
        "Shakespeare wrote 37 plays, 154 sonnets, and was born in 1564 in Stratford-upon-Avon.",
    ]
    results = await asyncio.gather(*[assess_composite_assertion(a) for a in assertions])
    for r in results:
        print(f"\n{r['assertion']}")
        print(f"  Confidence: {r['overall_confidence']:.2f} ({r['verdict']}) | {r['claim_count']} claims")
        for w in r["weak_claims"]:
            print(f"  [WEAK] {w['claim'][:80]} ({w['confidence']:.0%})")


asyncio.run(main())
```

## Solution 4: Domain-Specific Claim Router

Route each sub-claim to a specialized verifier based on domain (dates/numbers, geography, science, etc.).

```python
import asyncio
import json
import re
from dataclasses import dataclass
from enum import Enum
import anthropic

client = anthropic.AsyncAnthropic()
ROUTER_MODEL = "claude-haiku-4-5-20251001"
VERIFIER_MODEL = "claude-haiku-4-5-20251001"


class ClaimDomain(Enum):
    NUMERICAL = "numerical"
    DATE = "date"
    GEOGRAPHY = "geography"
    SCIENTIFIC = "scientific"
    BIOGRAPHICAL = "biographical"
    GENERAL = "general"


DOMAIN_SYSTEM_PROMPTS = {
    ClaimDomain.NUMERICAL: "You are a mathematical fact-checker. Focus on numerical accuracy.",
    ClaimDomain.DATE: "You are a historical date fact-checker. Focus on chronological accuracy.",
    ClaimDomain.GEOGRAPHY: "You are a geography fact-checker. Focus on location and spatial facts.",
    ClaimDomain.SCIENTIFIC: "You are a science fact-checker. Focus on scientific accuracy.",
    ClaimDomain.BIOGRAPHICAL: "You are a biographical fact-checker. Focus on people's life facts.",
    ClaimDomain.GENERAL: "You are a general fact-checker. Assess factual accuracy.",
}


@dataclass
class DomainClaim:
    claim: str
    domain: ClaimDomain = ClaimDomain.GENERAL
    confidence: float = 0.5
    explanation: str = ""


async def route_claim(claim: str) -> ClaimDomain:
    resp = await client.messages.create(
        model=ROUTER_MODEL,
        max_tokens=20,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Classify this claim's domain: '{claim}'\n"
                    f"Options: numerical, date, geography, scientific, biographical, general\n"
                    f"Reply with one word only."
                ),
            }
        ],
    )
    raw = resp.content[0].text.strip().lower()
    try:
        return ClaimDomain(raw)
    except ValueError:
        return ClaimDomain.GENERAL


async def verify_with_domain(claim_obj: DomainClaim) -> DomainClaim:
    system = DOMAIN_SYSTEM_PROMPTS[claim_obj.domain]
    resp = await client.messages.create(
        model=VERIFIER_MODEL,
        max_tokens=150,
        system=system,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Verify: '{claim_obj.claim}'\n"
                    f"Reply JSON: {{\"confidence\": 0-1, \"explanation\": str}}"
                ),
            }
        ],
    )
    try:
        match = re.search(r"\{[\s\S]+\}", resp.content[0].text)
        data = json.loads(match.group(0) if match else resp.content[0].text)
        claim_obj.confidence = float(data.get("confidence", 0.5))
        claim_obj.explanation = data.get("explanation", "")
    except Exception:
        claim_obj.confidence = 0.5
    return claim_obj


async def routed_verification(assertion: str) -> list[DomainClaim]:
    # Decompose
    resp = await client.messages.create(
        model=ROUTER_MODEL,
        max_tokens=200,
        messages=[{"role": "user", "content": f"List atomic claims, one per line:\n{assertion}"}],
    )
    claims_text = [l.strip() for l in resp.content[0].text.splitlines() if l.strip()]

    # Route in parallel
    domains = await asyncio.gather(*[route_claim(c) for c in claims_text])
    claim_objects = [DomainClaim(claim=c, domain=d) for c, d in zip(claims_text, domains)]

    # Verify in parallel
    return list(await asyncio.gather(*[verify_with_domain(co) for co in claim_objects]))


async def main():
    assertion = (
        "Marie Curie was born in 1867 in Warsaw, Poland, "
        "discovered polonium and radium, and was the first woman to win two Nobel Prizes."
    )
    results = await routed_verification(assertion)
    for r in results:
        flag = "OK" if r.confidence >= 0.7 else "WEAK"
        print(f"[{flag}:{r.domain.value}] {r.claim[:80]} → {r.confidence:.0%}")
        if r.explanation:
            print(f"  {r.explanation[:100]}")


asyncio.run(main())
```

## Solution 5: Contradictory Claim Detector

Find pairs of claims within the same assertion that logically contradict each other.

```python
import asyncio
import json
from dataclasses import dataclass
from itertools import combinations
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-haiku-4-5-20251001"


@dataclass
class Contradiction:
    claim_a: str
    claim_b: str
    explanation: str
    severity: str  # "definite" | "possible" | "none"


async def check_pair_contradiction(claim_a: str, claim_b: str) -> Contradiction:
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=150,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Do these two claims contradict each other?\n"
                    f"Claim A: {claim_a}\n"
                    f"Claim B: {claim_b}\n\n"
                    f"Reply JSON: {{\"severity\": \"definite|possible|none\", \"explanation\": str}}"
                ),
            }
        ],
    )
    try:
        import re
        match = re.search(r"\{[\s\S]+\}", resp.content[0].text)
        data = json.loads(match.group(0) if match else resp.content[0].text)
        return Contradiction(
            claim_a=claim_a,
            claim_b=claim_b,
            explanation=data.get("explanation", ""),
            severity=data.get("severity", "none"),
        )
    except Exception:
        return Contradiction(claim_a=claim_a, claim_b=claim_b, explanation="", severity="none")


async def detect_contradictions(assertion: str) -> list[Contradiction]:
    # Decompose
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=200,
        messages=[{"role": "user", "content": f"List atomic claims, one per line:\n{assertion}"}],
    )
    claims = [l.strip() for l in resp.content[0].text.splitlines() if l.strip()]

    if len(claims) < 2:
        return []

    # Check all pairs for contradictions (O(n²) but N is small)
    pairs = list(combinations(claims, 2))
    results = await asyncio.gather(*[check_pair_contradiction(a, b) for a, b in pairs])

    return [c for c in results if c.severity in ("definite", "possible")]


async def main():
    assertions = [
        # Internally consistent
        "Python was created in 1991 and released publicly in 1994.",
        # Internally contradictory (water boils at 100°C at sea level, not 80°C)
        "Water boils at 100°C at sea level, and in our experiment at sea level it boiled at 80°C.",
    ]

    for assertion in assertions:
        contradictions = await detect_contradictions(assertion)
        print(f"\nAssertion: {assertion[:80]}")
        if contradictions:
            for c in contradictions:
                print(f"  [CONTRADICTION:{c.severity}] {c.explanation}")
        else:
            print("  [OK] No internal contradictions found.")


asyncio.run(main())
```

## Solution 6: Incremental Claim Builder with Real-Time Validation

Validate each sub-claim as the agent generates them, stopping generation if an unsupported claim is produced.

```python
import asyncio
import json
import re
from dataclasses import dataclass, field
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-haiku-4-5-20251001"
VALIDATOR_MODEL = "claude-haiku-4-5-20251001"

CLAIM_BOUNDARY_PATTERNS = [
    r"(?<=[.!?])\s+(?=[A-Z])",  # Sentence boundary
    r",\s+and\s+",              # Coordinating conjunction
    r";\s+",                    # Semicolon
]


def split_into_claim_segments(text: str) -> list[str]:
    """Split text into claim-sized segments at sentence/clause boundaries."""
    for pattern in CLAIM_BOUNDARY_PATTERNS:
        text = re.sub(pattern, "\n", text)
    return [s.strip() for s in text.splitlines() if len(s.strip()) > 20]


@dataclass
class IncrementalVerification:
    original_query: str
    accepted_claims: list[str] = field(default_factory=list)
    rejected_claims: list[str] = field(default_factory=list)
    final_response: str = ""


async def validate_segment(segment: str) -> tuple[bool, str]:
    """Quickly validate a claim segment."""
    resp = await client.messages.create(
        model=VALIDATOR_MODEL,
        max_tokens=80,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Quick fact check: '{segment}'\n"
                    f"Reply: valid/invalid | reason (max 10 words)"
                ),
            }
        ],
    )
    text = resp.content[0].text.strip().lower()
    is_valid = not ("invalid" in text or "false" in text or "incorrect" in text)
    reason = text.split("|", 1)[1].strip() if "|" in text else ""
    return is_valid, reason


async def generate_with_incremental_validation(query: str) -> IncrementalVerification:
    result = IncrementalVerification(original_query=query)

    # Generate full response
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=500,
        messages=[{"role": "user", "content": query}],
    )
    full_text = resp.content[0].text

    # Split into segments and validate each
    segments = split_into_claim_segments(full_text)
    if not segments:
        result.accepted_claims.append(full_text)
        result.final_response = full_text
        return result

    validations = await asyncio.gather(*[validate_segment(s) for s in segments])

    for segment, (valid, reason) in zip(segments, validations):
        if valid:
            result.accepted_claims.append(segment)
        else:
            result.rejected_claims.append(f"{segment} [rejected: {reason}]")

    # Reconstruct response from accepted claims only
    result.final_response = " ".join(result.accepted_claims)
    return result


async def main():
    query = "Give me some historical facts about the development of the internet."
    result = await generate_with_incremental_validation(query)

    print(f"Query: {query}\n")
    print(f"Accepted claims ({len(result.accepted_claims)}):")
    for c in result.accepted_claims:
        print(f"  ✓ {c[:80]}")
    if result.rejected_claims:
        print(f"\nRejected claims ({len(result.rejected_claims)}):")
        for c in result.rejected_claims:
            print(f"  ✗ {c[:80]}")
    print(f"\nFiltered response:\n{result.final_response[:300]}")


asyncio.run(main())
```

## Comparison

| Solution | LLM calls | Detects | Granularity | Best for |
|---|---|---|---|---|
| **Claim decomposer** | 1 + N verify | Individual false claims | Atomic | General-purpose fact checking |
| **Dependency graph** | 1 + N verify | Cascading errors | Structural | Cause-effect assertions |
| **Confidence aggregator** | 1 + 1 batch | Weak composite claims | Statistical | Risk scoring |
| **Domain router** | N route + N verify | Domain-specific errors | Domain-aware | Mixed-domain assertions |
| **Contradiction detector** | 1 + N² pairs | Internal inconsistencies | Pairwise | Self-contradicting responses |
| **Incremental validator** | 1 generate + N | Real-time errors | Streaming | Live generation filtering |

Start with **claim decomposer** (Solution 1) for general-purpose hallucination detection. Add **contradiction detector** (Solution 5) when responses contain multiple related claims that could conflict. Use **confidence aggregator** (Solution 3) for a fast, single-pass reliability score.
