---
title: "Agent Doesn't Implement Multi-Agent Result Aggregation with Conflict Resolution"
description: "When multiple agents produce results for the same task, aggregate them intelligently — detecting conflicts, reconciling disagreements, and synthesizing a unified answer."
category: general
difficulty: advanced
tags: [multi-agent, aggregation, conflict-resolution, consensus, synthesis, voting]
---

# Agent Doesn't Implement Multi-Agent Result Aggregation with Conflict Resolution

## Problem

Running multiple agents in parallel produces multiple results that may agree, partially overlap, or directly conflict. Without explicit aggregation and conflict resolution, agents either arbitrarily pick one result or concatenate all of them into an incoherent response. Structured aggregation detects conflicts, evaluates which answer is best-supported, and synthesizes a coherent unified output.

---

## Option 1: Majority Voting with Conflict Detection

```python
import asyncio
import anthropic
import json
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class AgentResult:
    agent_id: str
    answer: str
    confidence: float = 0.5

async def run_agent(agent_id: str, question: str, persona: str) -> AgentResult:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=f"{persona}\nAfter your answer, on a new line write: CONFIDENCE: 0.X (0.0-1.0)",
        messages=[{"role": "user", "content": question}]
    )
    text = resp.content[0].text
    confidence = 0.5
    if "CONFIDENCE:" in text:
        try:
            parts = text.split("CONFIDENCE:")
            confidence = float(parts[-1].strip().split()[0])
            text = parts[0].strip()
        except Exception:
            pass
    return AgentResult(agent_id=agent_id, answer=text, confidence=confidence)

async def detect_conflict(results: list[AgentResult]) -> bool:
    """Use Haiku to check if results substantially disagree."""
    answers_text = "\n\n".join([f"Agent {r.agent_id}: {r.answer[:200]}" for r in results])
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=20,
        system="Answer only 'yes' or 'no'.",
        messages=[{"role": "user", "content": f"Do these agent answers substantially disagree on the facts?\n\n{answers_text}"}]
    )
    return "yes" in resp.content[0].text.lower()

async def aggregate_majority(question: str, results: list[AgentResult]) -> dict:
    # Group similar answers by semantic agreement
    has_conflict = await detect_conflict(results)
    if not has_conflict:
        # No conflict — pick highest-confidence result
        best = max(results, key=lambda r: r.confidence)
        return {"answer": best.answer, "method": "highest_confidence", "conflict": False, "winner": best.agent_id}

    # Conflict detected — synthesize with disagreement awareness
    answers_text = "\n\n".join([f"[{r.agent_id} confidence={r.confidence:.1f}]: {r.answer}" for r in results])
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system="""Multiple agents produced conflicting answers. Your job:
1. Identify the specific points of disagreement
2. Evaluate which answer is better-supported
3. Produce a unified, accurate answer that resolves the conflict
4. Note any genuine uncertainty where agents legitimately disagree""",
        messages=[{"role": "user", "content": f"Question: {question}\n\nAgent answers:\n{answers_text}"}]
    )
    return {"answer": resp.content[0].text, "method": "conflict_synthesis", "conflict": True}

async def multi_agent_answer(question: str) -> dict:
    personas = [
        ("agent_1", "You are a precise, conservative analyst. Only state what you're certain about."),
        ("agent_2", "You are a comprehensive expert. Provide thorough, complete answers."),
        ("agent_3", "You are a skeptical critic. Point out nuances and edge cases."),
    ]
    results = await asyncio.gather(*[run_agent(aid, question, persona) for aid, persona in personas])
    return await aggregate_majority(question, list(results))

async def main():
    questions = [
        "Is Python faster than Java for CPU-bound tasks?",
        "What is the capital of France?",
    ]
    for q in questions:
        result = await multi_agent_answer(q)
        print(f"Q: {q}")
        print(f"Method: {result['method']}, Conflict: {result['conflict']}")
        print(f"Answer: {result['answer'][:200]}\n")

asyncio.run(main())
```

---

## Option 2: Structured Field-Level Conflict Resolution

```python
import asyncio
import anthropic
import json
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

async def extract_structured(question: str, agent_id: str) -> dict:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=f'You are agent {agent_id}. Answer as JSON with keys: "main_answer", "key_facts": ["..."], "caveats": ["..."], "confidence": 0.0-1.0. Return only valid JSON.',
        messages=[{"role": "user", "content": question}]
    )
    try:
        return json.loads(resp.content[0].text)
    except Exception:
        return {"main_answer": resp.content[0].text, "key_facts": [], "caveats": [], "confidence": 0.5}

def merge_lists(lists: list[list], dedup: bool = True) -> list:
    combined = []
    for lst in lists:
        combined.extend(lst)
    if dedup:
        seen = set()
        return [x for x in combined if not (x.lower() in seen or seen.add(x.lower()))]
    return combined

async def field_level_aggregate(question: str, structured_results: list[dict]) -> dict:
    # For each field, check agreement
    all_answers = [r.get("main_answer", "") for r in structured_results]
    all_facts = merge_lists([r.get("key_facts", []) for r in structured_results])
    all_caveats = merge_lists([r.get("caveats", []) for r in structured_results])
    avg_confidence = sum(r.get("confidence", 0.5) for r in structured_results) / len(structured_results)

    # Reconcile main answers
    if len(set(a[:50].lower() for a in all_answers)) == 1:
        # All agents agree
        main_answer = all_answers[0]
        method = "unanimous"
    else:
        # Reconcile conflicting main answers
        answers_text = "\n".join([f"- {a}" for a in all_answers])
        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system="Reconcile these answers into one accurate, complete answer.",
            messages=[{"role": "user", "content": f"Question: {question}\n\nAnswers:\n{answers_text}"}]
        )
        main_answer = resp.content[0].text
        method = "reconciled"

    return {
        "main_answer": main_answer,
        "key_facts": all_facts[:10],
        "caveats": all_caveats[:5],
        "confidence": avg_confidence,
        "method": method,
        "agent_count": len(structured_results),
    }

async def structured_multi_agent(question: str, n_agents: int = 3) -> dict:
    results = await asyncio.gather(*[extract_structured(question, f"agent_{i}") for i in range(n_agents)])
    return await field_level_aggregate(question, list(results))

async def main():
    result = await structured_multi_agent("What are the main advantages of using TypeScript over JavaScript?")
    print(f"Method: {result['method']}, Confidence: {result['confidence']:.2f}")
    print(f"Answer: {result['main_answer'][:200]}")
    print(f"Key facts: {result['key_facts'][:3]}")

asyncio.run(main())
```

---

## Option 3: Weighted Ensemble with Source Attribution

```python
import asyncio
import anthropic
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class WeightedResult:
    agent_id: str
    role: str
    answer: str
    weight: float
    strengths: list[str] = field(default_factory=list)

AGENT_ROLES = [
    ("fact_checker", "Focus on factual accuracy only. Correct any errors.", 1.5),
    ("comprehensive", "Provide the most complete answer possible.", 1.0),
    ("concise", "Give the essential answer in as few words as possible.", 0.8),
    ("examples", "Illustrate with concrete examples.", 0.9),
]

async def run_weighted_agent(question: str, role: str, instruction: str, weight: float) -> WeightedResult:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=instruction,
        messages=[{"role": "user", "content": question}]
    )
    return WeightedResult(agent_id=role, role=role, answer=resp.content[0].text, weight=weight)

async def weighted_synthesis(question: str, results: list[WeightedResult]) -> str:
    # Build weighted context — higher weight agents' answers appear first and are labeled
    weighted_answers = sorted(results, key=lambda r: -r.weight)
    context = "\n\n".join([f"[{r.role.upper()} weight={r.weight}]:\n{r.answer}" for r in weighted_answers])
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=700,
        system="""Synthesize these weighted agent answers into one optimal response.
Give more weight to higher-weighted agents, especially the fact_checker.
Resolve any conflicts by deferring to the highest-weight accurate response.
Include the best examples from example-focused agents.""",
        messages=[{"role": "user", "content": f"Question: {question}\n\nWeighted answers:\n{context}"}]
    )
    return resp.content[0].text

async def weighted_ensemble(question: str) -> dict:
    results = await asyncio.gather(*[run_weighted_agent(question, role, instr, weight) for role, instr, weight in AGENT_ROLES])
    synthesis = await weighted_synthesis(question, list(results))
    total_weight = sum(r.weight for r in results)
    return {
        "answer": synthesis,
        "agents_used": len(results),
        "total_weight": total_weight,
        "contributions": [{"role": r.role, "weight": r.weight} for r in sorted(results, key=lambda r: -r.weight)]
    }

async def main():
    result = await weighted_ensemble("How does garbage collection work in Python?")
    print(f"Answer ({result['agents_used']} agents):\n{result['answer'][:300]}")
    print(f"Contributions: {result['contributions']}")

asyncio.run(main())
```

---

## Option 4: Bayesian Belief Updating Across Agents

```python
import asyncio
import anthropic
import json
import math
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class Claim:
    text: str
    probability: float = 0.5  # prior
    supporting_agents: list[str] = field(default_factory=list)
    opposing_agents: list[str] = field(default_factory=list)

    def update(self, agent_id: str, supports: bool, agent_reliability: float = 0.75):
        """Bayesian update: P(claim|evidence) ∝ P(evidence|claim) * P(claim)"""
        p_e_given_h = agent_reliability if supports else (1 - agent_reliability)
        p_e_given_not_h = (1 - agent_reliability) if supports else agent_reliability
        posterior = (p_e_given_h * self.probability) / (
            p_e_given_h * self.probability + p_e_given_not_h * (1 - self.probability)
        )
        self.probability = posterior
        if supports:
            self.supporting_agents.append(agent_id)
        else:
            self.opposing_agents.append(agent_id)

async def extract_claims(question: str, agent_id: str) -> list[str]:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system='List 3-5 key factual claims as JSON array of strings. Each claim should be independently evaluable.',
        messages=[{"role": "user", "content": question}]
    )
    try:
        return json.loads(resp.content[0].text)
    except Exception:
        return [resp.content[0].text]

async def evaluate_claim(claim_text: str, agent_id: str) -> tuple[bool, float]:
    """Returns (supports, reliability_score)."""
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=60,
        system='Answer: {"supports": true/false, "reliability": 0.0-1.0}. JSON only.',
        messages=[{"role": "user", "content": f"Is this claim accurate? '{claim_text}'"}]
    )
    try:
        data = json.loads(resp.content[0].text)
        return bool(data["supports"]), float(data.get("reliability", 0.75))
    except Exception:
        return True, 0.5

async def bayesian_aggregate(question: str, n_agents: int = 3) -> dict:
    # Phase 1: All agents extract claims
    all_claims_lists = await asyncio.gather(*[extract_claims(question, f"agent_{i}") for i in range(n_agents)])

    # Deduplicate claims
    all_claim_texts: set[str] = set()
    for claims_list in all_claims_lists:
        all_claim_texts.update(claims_list)

    claims = {ct: Claim(text=ct) for ct in all_claim_texts}

    # Phase 2: Each agent evaluates each claim
    eval_tasks = []
    for agent_idx in range(n_agents):
        for claim_text, claim in claims.items():
            eval_tasks.append((f"agent_{agent_idx}", claim_text, evaluate_claim(claim_text, f"agent_{agent_idx}")))

    results = await asyncio.gather(*[t[2] for t in eval_tasks], return_exceptions=True)
    for (agent_id, claim_text, _), result in zip(eval_tasks, results):
        if isinstance(result, tuple):
            supports, reliability = result
            claims[claim_text].update(agent_id, supports, reliability)

    # Phase 3: Keep high-probability claims, synthesize
    accepted = {ct: c for ct, c in claims.items() if c.probability >= 0.6}
    rejected = {ct: c for ct, c in claims.items() if c.probability < 0.4}

    accepted_text = "\n".join([f"- [{c.probability:.0%}] {ct}" for ct, c in sorted(accepted.items(), key=lambda x: -x[1].probability)])
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system="Synthesize these verified claims (with confidence scores) into a coherent answer.",
        messages=[{"role": "user", "content": f"Question: {question}\n\nVerified claims:\n{accepted_text}"}]
    )

    return {
        "answer": resp.content[0].text,
        "total_claims": len(claims),
        "accepted_claims": len(accepted),
        "rejected_claims": len(rejected),
        "high_confidence": [ct for ct, c in accepted.items() if c.probability > 0.85]
    }

async def main():
    result = await bayesian_aggregate("What are the performance characteristics of Python's asyncio?")
    print(f"Claims: {result['total_claims']} total, {result['accepted_claims']} accepted, {result['rejected_claims']} rejected")
    print(f"High confidence: {result['high_confidence'][:3]}")
    print(f"Answer: {result['answer'][:250]}")

asyncio.run(main())
```

---

## Option 5: Cascading Specialist Refinement

```python
import asyncio
import anthropic
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

@dataclass
class RefinementStage:
    name: str
    instruction: str
    model: str = "claude-haiku-4-5-20251001"
    max_tokens: int = 500

PIPELINE = [
    RefinementStage(
        "generator",
        "Generate a comprehensive initial answer to the question.",
        max_tokens=600
    ),
    RefinementStage(
        "fact_checker",
        "Review this answer for factual accuracy. Correct any errors. Keep all correct content.",
        max_tokens=600
    ),
    RefinementStage(
        "clarity_editor",
        "Improve the clarity and structure of this answer without changing its meaning.",
        max_tokens=600
    ),
    RefinementStage(
        "completeness_reviewer",
        "Check if anything important is missing. Add any critical missing information.",
        model="claude-sonnet-4-6",
        max_tokens=700
    ),
]

async def cascading_refinement(question: str) -> dict:
    current_answer = ""
    history: list[dict] = [{"role": "user", "content": question}]
    stages_log: list[dict] = []

    for stage in PIPELINE:
        if current_answer:
            prompt = f"Previous answer:\n{current_answer}\n\nYour task: {stage.instruction}"
        else:
            prompt = question

        resp = await client.messages.create(
            model=stage.model,
            max_tokens=stage.max_tokens,
            system=f"You are a {stage.name}. {stage.instruction}",
            messages=[{"role": "user", "content": prompt}]
        )
        current_answer = resp.content[0].text
        stages_log.append({"stage": stage.name, "output_length": len(current_answer)})
        print(f"[CASCADE] {stage.name}: {len(current_answer)} chars")

    return {"answer": current_answer, "stages": stages_log}

async def main():
    result = await cascading_refinement("Explain the differences between processes and threads in operating systems.")
    print(f"\nFinal answer ({len(result['answer'])} chars):\n{result['answer'][:300]}")

asyncio.run(main())
```

---

## Option 6: Tournament-Style Bracket Elimination

```python
import asyncio
import anthropic
from dataclasses import dataclass
import math

client = anthropic.AsyncAnthropic()

@dataclass
class Contestant:
    agent_id: str
    answer: str

async def generate_answer(question: str, agent_id: str, variation: str) -> Contestant:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=f"You are agent {agent_id}. {variation}",
        messages=[{"role": "user", "content": question}]
    )
    return Contestant(agent_id=agent_id, answer=resp.content[0].text)

async def judge_matchup(question: str, a: Contestant, b: Contestant) -> Contestant:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        system='Which answer better addresses the question? Reply only "A" or "B".',
        messages=[{"role": "user", "content": f"Question: {question}\n\nA: {a.answer[:300]}\n\nB: {b.answer[:300]}"}]
    )
    winner_label = resp.content[0].text.strip().upper()
    return a if "A" in winner_label else b

async def tournament(question: str, contestants: list[Contestant]) -> Contestant:
    """Single-elimination tournament."""
    current_round = contestants
    round_num = 0
    while len(current_round) > 1:
        round_num += 1
        next_round: list[Contestant] = []
        pairs = list(zip(current_round[::2], current_round[1::2]))
        if len(current_round) % 2 == 1:
            next_round.append(current_round[-1])  # bye

        winners = await asyncio.gather(*[judge_matchup(question, a, b) for a, b in pairs])
        next_round.extend(winners)
        print(f"[TOURNAMENT] Round {round_num}: {len(current_round)} → {len(next_round)}")
        current_round = next_round

    return current_round[0]

async def tournament_aggregate(question: str, n_agents: int = 8) -> dict:
    variations = [
        "Be precise and concise.", "Be comprehensive.", "Use examples.",
        "Focus on practical implications.", "Be formal and academic.",
        "Be simple and accessible.", "Highlight trade-offs.", "Give structured analysis."
    ]
    contestants = await asyncio.gather(*[
        generate_answer(question, f"agent_{i}", variations[i % len(variations)])
        for i in range(n_agents)
    ])
    winner = await tournament(question, list(contestants))
    print(f"[TOURNAMENT] Winner: {winner.agent_id}")
    return {"answer": winner.answer, "winner": winner.agent_id, "contestants": n_agents}

async def main():
    result = await tournament_aggregate(
        "What is the best way to handle errors in async Python code?",
        n_agents=4
    )
    print(f"\nWinner ({result['winner']}) from {result['contestants']} contestants:")
    print(result["answer"][:300])

asyncio.run(main())
```

---

## Comparison

| Option | Conflict Detection | Synthesis Method | API Overhead | Best For |
|--------|------------------|----------------|-------------|----------|
| 1 – Majority Voting | LLM conflict check | Haiku detector + Sonnet synthesis | Medium | General Q&A |
| 2 – Field-Level | Per-field comparison | Field merge + reconcile | Medium | Structured data extraction |
| 3 – Weighted Ensemble | Implicit via weighting | Weight-aware synthesis | Medium | Role-specialized agents |
| 4 – Bayesian Updating | Claim-level probability | Claim filtering + synthesis | High | Factual research tasks |
| 5 – Cascading Refinement | Per-stage correction | Sequential refinement | Medium | Document generation |
| 6 – Tournament | Pairwise judging | Elimination bracket | High | Best-of-N selection |

**Recommendation:** Use Option 1 for most use cases — it balances quality and cost. Use Option 3 (weighted ensemble) when you have specialized agents with known strengths. Reserve Option 4 (Bayesian) for high-stakes factual questions where accuracy is paramount and API cost is secondary.
