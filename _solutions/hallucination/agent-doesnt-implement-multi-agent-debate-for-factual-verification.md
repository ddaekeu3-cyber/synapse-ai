---
title: "Agent Doesn't Implement Multi-Agent Debate for Factual Verification"
description: "Run multiple agents that argue opposing positions on factual claims, then synthesize a more accurate answer from the adversarial exchange."
category: hallucination
difficulty: advanced
tags: [hallucination, debate, multi-agent, verification, factual, asyncio]
---

# Agent Doesn't Implement Multi-Agent Debate for Factual Verification

## Problem

A single LLM confidently states incorrect facts with no internal mechanism to challenge its own claims. Multi-agent debate forces agents to argue different positions on the same claim — surface contradictions, demand evidence, and expose confident errors before they reach the user.

---

## Option 1: Two-Agent Claim-and-Challenge Debate

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

async def proposer(claim: str, context: str = "") -> str:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system="You are the Proposer. Argue clearly and confidently in support of the given claim. Cite reasoning and evidence.",
        messages=[{"role": "user", "content": f"Claim: {claim}\nContext: {context}\n\nDefend this claim."}]
    )
    return resp.content[0].text

async def challenger(claim: str, proposer_arg: str) -> str:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system="You are the Challenger. Identify weaknesses, counterexamples, and factual errors in the argument. Be precise.",
        messages=[{"role": "user", "content": f"Claim: {claim}\n\nProposer's argument:\n{proposer_arg}\n\nChallenge this argument."}]
    )
    return resp.content[0].text

async def judge(claim: str, proposer_arg: str, challenger_arg: str) -> dict:
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system="""You are the Judge. Evaluate both sides and determine:
1. The most accurate assessment of the claim (true/false/partially true/uncertain)
2. The key evidence that supports your verdict
3. Any important caveats or nuances
Return structured reasoning.""",
        messages=[{"role": "user", "content": f"Claim: {claim}\n\nProposer:\n{proposer_arg}\n\nChallenger:\n{challenger_arg}\n\nVerdict?"}]
    )
    return {"verdict": resp.content[0].text, "claim": claim}

async def debate(claim: str, context: str = "") -> dict:
    prop_arg = await proposer(claim, context)
    chall_arg = await challenger(claim, prop_arg)
    verdict = await judge(claim, prop_arg, chall_arg)
    return {
        **verdict,
        "proposer_argument": prop_arg,
        "challenger_argument": chall_arg,
    }

async def main():
    claims = [
        "Python was created in the 1980s.",
        "Asyncio uses multiple OS threads to achieve parallelism.",
        "The GIL prevents true parallel execution of Python threads.",
    ]
    for claim in claims:
        result = await debate(claim)
        print(f"\nClaim: {claim}")
        print(f"Verdict: {result['verdict'][:200]}\n")

asyncio.run(main())
```

---

## Option 2: Parallel Multi-Position Debate with Synthesis

```python
import asyncio
import anthropic
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

POSITIONS = [
    ("Advocate", "Argue strongly in favor of the claim with all supporting evidence."),
    ("Skeptic", "Question the claim rigorously. Demand evidence for every assertion."),
    ("Devil's Advocate", "Find the most damaging counterexample or edge case that challenges the claim."),
]

@dataclass
class Position:
    role: str
    argument: str

async def argue(claim: str, role: str, instruction: str) -> Position:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=350,
        system=f"You are the {role}. {instruction} Be concise but precise.",
        messages=[{"role": "user", "content": f"Claim to evaluate: {claim}"}]
    )
    return Position(role=role, argument=resp.content[0].text)

async def synthesize(claim: str, positions: list[Position]) -> str:
    debate_text = "\n\n".join([f"**{p.role}:**\n{p.argument}" for p in positions])
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=700,
        system="""You are a neutral fact-checker synthesizing a multi-position debate.
Determine the most accurate answer. Explicitly state:
- Your confidence level (high/medium/low)
- Whether the claim is: TRUE / FALSE / PARTIALLY TRUE / UNCERTAIN
- The key reasoning that led to your conclusion
- Any important caveats""",
        messages=[{"role": "user", "content": f"Claim: {claim}\n\nDebate:\n{debate_text}"}]
    )
    return resp.content[0].text

async def multi_debate(claim: str) -> dict:
    # Run all positions in parallel
    positions = await asyncio.gather(*[argue(claim, role, inst) for role, inst in POSITIONS])
    synthesis = await synthesize(claim, list(positions))
    return {
        "claim": claim,
        "positions": {p.role: p.argument for p in positions},
        "synthesis": synthesis,
    }

async def main():
    result = await multi_debate("Transformers are based on recurrent neural network architecture.")
    print(f"Claim: {result['claim']}")
    for role, arg in result["positions"].items():
        print(f"\n[{role}]: {arg[:100]}...")
    print(f"\nSynthesis:\n{result['synthesis']}")

asyncio.run(main())
```

---

## Option 3: Iterative Debate Rounds with Convergence Detection

```python
import asyncio
import anthropic
import json

client = anthropic.AsyncAnthropic()

async def debate_round(claim: str, history: list[dict], agent_role: str, opposing_arg: str | None) -> str:
    system = f"""You are debating whether the following claim is accurate: "{claim}"
Your role: {agent_role}
{"Respond to the opposing argument below." if opposing_arg else "Make your opening argument."}
Be specific. Point to concrete facts. Limit to 3 key points."""

    messages = [{"role": "user", "content": opposing_arg or "Make your opening argument."}]
    if history:
        messages = history + messages

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=350,
        system=system,
        messages=messages
    )
    return resp.content[0].text

async def check_convergence(claim: str, arg_a: str, arg_b: str) -> bool:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        system='Answer only "yes" or "no".',
        messages=[{"role": "user", "content": f"Do these two arguments substantially agree on the facts?\n\nArg A: {arg_a}\n\nArg B: {arg_b}"}]
    )
    return "yes" in resp.content[0].text.lower()

async def iterative_debate(claim: str, max_rounds: int = 3) -> dict:
    history_a: list[dict] = []
    history_b: list[dict] = []
    arg_a = arg_b = None
    rounds = []

    for round_num in range(max_rounds):
        # Both agents argue simultaneously
        arg_a, arg_b = await asyncio.gather(
            debate_round(claim, history_a, "Fact Supporter — argue the claim is accurate", arg_b),
            debate_round(claim, history_b, "Fact Challenger — argue the claim is inaccurate or needs qualification", arg_a),
        )
        rounds.append({"round": round_num + 1, "supporter": arg_a, "challenger": arg_b})

        # Update histories
        history_a.extend([{"role": "assistant", "content": arg_a}, {"role": "user", "content": arg_b}])
        history_b.extend([{"role": "assistant", "content": arg_b}, {"role": "user", "content": arg_a}])

        # Check if debate has converged
        converged = await check_convergence(claim, arg_a, arg_b)
        if converged:
            print(f"[DEBATE] Converged after round {round_num + 1}")
            break

    # Final verdict
    debate_summary = "\n\n".join([f"Round {r['round']}:\nSupporter: {r['supporter']}\nChallenger: {r['challenger']}" for r in rounds])
    verdict_resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system="Synthesize the debate and deliver a final, calibrated verdict on the claim's accuracy.",
        messages=[{"role": "user", "content": f"Claim: {claim}\n\n{debate_summary}"}]
    )

    return {
        "claim": claim,
        "rounds": len(rounds),
        "rounds_detail": rounds,
        "verdict": verdict_resp.content[0].text,
    }

async def main():
    result = await iterative_debate("Neural networks require GPUs to function.")
    print(f"Claim: {result['claim']}")
    print(f"Rounds: {result['rounds']}")
    print(f"Verdict: {result['verdict'][:300]}")

asyncio.run(main())
```

---

## Option 4: Structured Socratic Questioning Chain

```python
import asyncio
import anthropic
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class SocraticExchange:
    question: str
    answer: str
    challenges_revealed: list[str] = field(default_factory=list)

async def generate_socratic_questions(claim: str, previous_answers: list[str]) -> list[str]:
    context = "\n".join(previous_answers[-3:]) if previous_answers else "No prior context."
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system='Generate 3 probing Socratic questions that could reveal hidden assumptions or errors in the claim. Return as JSON array of strings.',
        messages=[{"role": "user", "content": f"Claim: {claim}\nContext so far: {context}"}]
    )
    try:
        import json
        return json.loads(resp.content[0].text)
    except Exception:
        return ["Is this always true?", "What evidence supports this?", "Are there counterexamples?"]

async def answer_question(claim: str, question: str) -> str:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system="Answer the question precisely and honestly. If the answer reveals a flaw in the original claim, say so explicitly.",
        messages=[{"role": "user", "content": f"Original claim: {claim}\n\nQuestion: {question}"}]
    )
    return resp.content[0].text

async def socratic_verification(claim: str, depth: int = 3) -> dict:
    exchanges: list[SocraticExchange] = []
    all_answers: list[str] = []

    for round_num in range(depth):
        questions = await generate_socratic_questions(claim, all_answers)
        # Answer all questions in parallel
        answers = await asyncio.gather(*[answer_question(claim, q) for q in questions])

        for q, a in zip(questions, answers):
            exchanges.append(SocraticExchange(question=q, answer=a))
            all_answers.append(a)

    # Synthesize verdict from all Q&A exchanges
    qa_text = "\n\n".join([f"Q: {e.question}\nA: {e.answer}" for e in exchanges])
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system="Based on the Socratic examination, provide a final verdict on the claim's accuracy, confidence level, and key qualifications.",
        messages=[{"role": "user", "content": f"Claim: {claim}\n\nSocratic Examination:\n{qa_text}"}]
    )
    return {
        "claim": claim,
        "exchanges": [{"q": e.question, "a": e.answer} for e in exchanges],
        "verdict": resp.content[0].text,
    }

async def main():
    result = await socratic_verification(
        "Machine learning models understand the meaning of text.",
        depth=2
    )
    print(f"Claim: {result['claim']}\n")
    for ex in result["exchanges"][:3]:
        print(f"Q: {ex['q']}\nA: {ex['a'][:100]}...\n")
    print(f"Verdict: {result['verdict'][:300]}")

asyncio.run(main())
```

---

## Option 5: Red Team + Blue Team Adversarial Verification

```python
import asyncio
import anthropic
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

@dataclass
class TeamResult:
    team: str
    strategy: str
    findings: str

async def red_team(claim: str) -> TeamResult:
    """Actively tries to disprove the claim."""
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        system="""You are a Red Team agent. Your ONLY goal is to find ways the claim could be WRONG.
Strategies: find counterexamples, identify unstated assumptions, challenge definitions, find edge cases, check for recency (is this outdated?).
Report your best attacks on the claim's validity.""",
        messages=[{"role": "user", "content": f"Claim to attack: {claim}"}]
    )
    return TeamResult(team="Red", strategy="Adversarial disproof", findings=resp.content[0].text)

async def blue_team(claim: str, red_findings: str) -> TeamResult:
    """Defends the claim against red team attacks."""
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        system="""You are a Blue Team agent. Defend the claim against the Red Team's attacks.
For each Red Team attack: either refute it with evidence, concede it and qualify the claim, or acknowledge it as a genuine limitation.
Produce the most accurate version of the claim after considering all attacks.""",
        messages=[{"role": "user", "content": f"Original claim: {claim}\n\nRed Team attacks:\n{red_findings}"}]
    )
    return TeamResult(team="Blue", strategy="Adversarial defense", findings=resp.content[0].text)

async def neutral_arbiter(claim: str, red: TeamResult, blue: TeamResult) -> dict:
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system="""You are a neutral arbiter. Evaluate the red-blue exchange.
Determine:
1. Which attacks were valid and which were refuted
2. The most accurate, qualified version of the claim
3. Confidence: HIGH (clear evidence), MEDIUM (reasonable but caveats), LOW (uncertain or contested)
4. A one-sentence summary a user can trust""",
        messages=[{"role": "user", "content": f"Claim: {claim}\n\nRed Team:\n{red.findings}\n\nBlue Team:\n{blue.findings}"}]
    )
    return {
        "claim": claim,
        "red_findings": red.findings,
        "blue_findings": blue.findings,
        "arbiter_verdict": resp.content[0].text,
    }

async def red_blue_verify(claim: str) -> dict:
    red = await red_team(claim)
    blue = await blue_team(claim, red.findings)
    return await neutral_arbiter(claim, red, blue)

async def main():
    claims = [
        "Attention is all you need — modern LLMs don't use any recurrent components.",
        "Python is faster than JavaScript for CPU-bound tasks.",
    ]
    results = await asyncio.gather(*[red_blue_verify(c) for c in claims])
    for r in results:
        print(f"\nClaim: {r['claim']}\nVerdict: {r['arbiter_verdict'][:250]}\n{'='*60}")

asyncio.run(main())
```

---

## Option 6: Claim Decomposition + Parallel Sub-Claim Debate

```python
import asyncio
import anthropic
import json
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

@dataclass
class SubClaimVerdict:
    sub_claim: str
    verdict: str  # TRUE/FALSE/UNCERTAIN
    confidence: str  # HIGH/MEDIUM/LOW
    reasoning: str

async def decompose(claim: str) -> list[str]:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system='Break the claim into 3-5 atomic, independently verifiable sub-claims. Return as JSON array of strings.',
        messages=[{"role": "user", "content": f"Claim: {claim}"}]
    )
    try:
        return json.loads(resp.content[0].text)
    except Exception:
        return [claim]

async def verify_sub_claim(sub_claim: str) -> SubClaimVerdict:
    # Two-agent mini-debate per sub-claim
    for_resp, against_resp = await asyncio.gather(
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system="Argue FOR this claim's accuracy in 2-3 sentences.",
            messages=[{"role": "user", "content": sub_claim}]
        ),
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system="Argue AGAINST this claim's accuracy in 2-3 sentences.",
            messages=[{"role": "user", "content": sub_claim}]
        )
    )

    verdict_resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system='Evaluate both arguments. Return JSON: {"verdict": "TRUE|FALSE|UNCERTAIN", "confidence": "HIGH|MEDIUM|LOW", "reasoning": "one sentence"}',
        messages=[{"role": "user", "content": f"Sub-claim: {sub_claim}\n\nFor: {for_resp.content[0].text}\nAgainst: {against_resp.content[0].text}"}]
    )
    try:
        data = json.loads(verdict_resp.content[0].text)
        return SubClaimVerdict(
            sub_claim=sub_claim,
            verdict=data.get("verdict", "UNCERTAIN"),
            confidence=data.get("confidence", "LOW"),
            reasoning=data.get("reasoning", "")
        )
    except Exception:
        return SubClaimVerdict(sub_claim=sub_claim, verdict="UNCERTAIN", confidence="LOW", reasoning="Parse error")

async def aggregate(claim: str, verdicts: list[SubClaimVerdict]) -> str:
    summary = "\n".join([f"- [{v.verdict}/{v.confidence}] {v.sub_claim}: {v.reasoning}" for v in verdicts])
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        system="Given sub-claim verdicts, provide an overall verdict on the original claim.",
        messages=[{"role": "user", "content": f"Original claim: {claim}\n\nSub-claim verdicts:\n{summary}"}]
    )
    return resp.content[0].text

async def decomposed_debate(claim: str) -> dict:
    sub_claims = await decompose(claim)
    verdicts = await asyncio.gather(*[verify_sub_claim(sc) for sc in sub_claims])
    overall = await aggregate(claim, list(verdicts))
    return {
        "claim": claim,
        "sub_claims": [{"claim": v.sub_claim, "verdict": v.verdict, "confidence": v.confidence} for v in verdicts],
        "overall": overall
    }

async def main():
    result = await decomposed_debate(
        "BERT is a GPT-based model that uses causal attention and is trained on next-token prediction."
    )
    print(f"Claim: {result['claim']}\n")
    for sc in result["sub_claims"]:
        print(f"  [{sc['verdict']}/{sc['confidence']}] {sc['claim']}")
    print(f"\nOverall: {result['overall'][:300]}")

asyncio.run(main())
```

---

## Comparison

| Option | Debate Structure | Agents | Rounds | Best For |
|--------|----------------|--------|--------|----------|
| 1 – Claim-and-Challenge | Proposer → Challenger → Judge | 3 | 1 | Simple fact checks |
| 2 – Multi-Position Parallel | 3 parallel roles → Synthesizer | 4 | 1 | Nuanced factual claims |
| 3 – Iterative Rounds | Supporter ↔ Challenger (N rounds) | 3 | N (up to 3) | Complex contested claims |
| 4 – Socratic Chain | Questioner → Answerer (depth × 3 Qs) | 2 | depth | Assumption surfacing |
| 5 – Red/Blue Team | Red attacks → Blue defends → Arbiter | 3 | 2 | Adversarial verification |
| 6 – Decomposed | Split → parallel per-claim debate | 3N+1 | 1 per sub-claim | Compound factual statements |

**Recommendation:** Use Option 2 for most fact-checking — three parallel positions plus synthesis balances cost and accuracy. Use Option 6 for compound claims with multiple independently verifiable facts. Reserve Option 3's iterative rounds for high-stakes decisions where token cost is secondary to accuracy.
