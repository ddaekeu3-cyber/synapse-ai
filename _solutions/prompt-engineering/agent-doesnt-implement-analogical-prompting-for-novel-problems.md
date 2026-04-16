---
title: "Agent Doesn't Implement Analogical Prompting for Novel Problems"
description: "Generate or retrieve analogous solved problems before tackling a novel one — helping the model transfer solution patterns from familiar to unfamiliar domains."
category: prompt-engineering
difficulty: intermediate
tags: [prompt-engineering, analogical-reasoning, few-shot, problem-solving, transfer-learning, reasoning]
---

# Agent Doesn't Implement Analogical Prompting for Novel Problems

## Problem

When agents encounter novel problems, they attempt them cold — without connecting the problem to similar solved problems where solution strategies are well-established. Analogical prompting explicitly surfaces structural similarities between the new problem and known solved problems, dramatically improving reasoning quality on unfamiliar tasks.

---

## Option 1: Self-Generated Analogies Before Solving

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

async def solve_with_analogy(problem: str) -> dict:
    # Step 1: Generate analogous problems from familiar domains
    analogy_resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system="""Before solving a problem, identify 2-3 analogous problems from well-understood domains.
For each analogy:
1. Name the analogous problem
2. Describe how its structure mirrors the current problem
3. State the key solution insight from the analogy""",
        messages=[{"role": "user", "content": f"Problem to solve:\n{problem}\n\nGenerate analogies first."}]
    )
    analogies = analogy_resp.content[0].text

    # Step 2: Use analogies to guide solution
    solution_resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=700,
        system="Use the identified analogies to inform your solution strategy.",
        messages=[
            {"role": "user", "content": f"Problem:\n{problem}\n\nAnalogies:\n{analogies}\n\nNow solve the problem, explicitly drawing on the analogical insights."}
        ]
    )
    return {
        "problem": problem,
        "analogies": analogies,
        "solution": solution_resp.content[0].text
    }

async def main():
    problems = [
        "How should a distributed system handle a 'split-brain' scenario where network partitions cause nodes to disagree on the current leader?",
        "Design an algorithm to fairly allocate limited GPU resources among competing ML training jobs with different priorities.",
    ]
    for problem in problems:
        result = await solve_with_analogy(problem)
        print(f"Problem: {problem[:80]}...\n")
        print(f"Analogies:\n{result['analogies'][:300]}...\n")
        print(f"Solution:\n{result['solution'][:300]}...\n{'='*60}\n")

asyncio.run(main())
```

---

## Option 2: Retrieved Analogies from a Problem Library

```python
import asyncio
import anthropic
import hashlib
import math
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class SolvedProblem:
    id: str
    domain: str
    problem: str
    solution: str
    key_insight: str
    tags: list[str] = field(default_factory=list)

PROBLEM_LIBRARY = [
    SolvedProblem(
        id="p1", domain="logistics",
        problem="How to route delivery trucks to minimize total distance?",
        solution="Use dynamic programming with memoization on subsets of destinations.",
        key_insight="Decompose into overlapping subproblems; optimal substructure exists.",
        tags=["optimization", "graph", "routing"]
    ),
    SolvedProblem(
        id="p2", domain="biology",
        problem="How do cells coordinate to form organs without central control?",
        solution="Local signaling rules + feedback loops produce emergent global structure.",
        key_insight="Complex global behavior emerges from simple local rules.",
        tags=["distributed", "emergence", "self-organization"]
    ),
    SolvedProblem(
        id="p3", domain="economics",
        problem="How to design auctions that truthfully reveal bidder valuations?",
        solution="Vickrey (second-price) auction makes truthful bidding the dominant strategy.",
        key_insight="Mechanism design: align incentives so honest behavior is optimal.",
        tags=["incentives", "mechanism-design", "game-theory"]
    ),
    SolvedProblem(
        id="p4", domain="physics",
        problem="How to predict particle positions when initial conditions are uncertain?",
        solution="Use probabilistic distributions (wave functions) rather than exact trajectories.",
        key_insight="Embrace uncertainty explicitly; model distributions, not point estimates.",
        tags=["uncertainty", "probability", "estimation"]
    ),
    SolvedProblem(
        id="p5", domain="military",
        problem="How to coordinate an attack when communication between units may be compromised?",
        solution="Two Generals Problem: use acknowledgment chains and timeouts; accept bounded uncertainty.",
        key_insight="In unreliable communication, guarantee eventual consistency, not perfect sync.",
        tags=["distributed", "consensus", "reliability"]
    ),
]

def embed(text: str, dim: int = 64) -> list[float]:
    vec = [0.0] * dim
    for word in text.lower().split():
        h = int(hashlib.sha256(word.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(x*x for x in vec)) or 1.0
    return [x/norm for x in vec]

def cosine(a: list[float], b: list[float]) -> float:
    return sum(x*y for x,y in zip(a,b))

def retrieve_analogies(query: str, k: int = 2) -> list[SolvedProblem]:
    q_emb = embed(query)
    scored = [(cosine(q_emb, embed(p.problem + " " + " ".join(p.tags))), p) for p in PROBLEM_LIBRARY]
    return [p for _, p in sorted(scored, key=lambda x: -x[0])[:k]]

async def solve_with_retrieved_analogies(problem: str) -> dict:
    analogies = retrieve_analogies(problem, k=2)
    analogy_text = "\n\n".join([
        f"**{p.domain.upper()} — {p.problem}**\nSolution: {p.solution}\nKey insight: {p.key_insight}"
        for p in analogies
    ])
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=700,
        system=f"""You have access to these analogous solved problems:

{analogy_text}

Use the structural insights from these analogies to guide your solution.
Explicitly state which analogical insight you are applying and why.""",
        messages=[{"role": "user", "content": f"Novel problem to solve:\n{problem}"}]
    )
    return {"problem": problem, "analogies_used": [p.id for p in analogies], "solution": resp.content[0].text}

async def main():
    problem = "How should a multi-agent AI system handle disagreements between agents about the correct action when they can't communicate reliably?"
    result = await solve_with_retrieved_analogies(problem)
    print(f"Analogies used: {result['analogies_used']}")
    print(f"Solution:\n{result['solution'][:400]}")

asyncio.run(main())
```

---

## Option 3: Cross-Domain Structure Mapping

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

async def structure_map(source_domain: str, target_problem: str) -> str:
    """Explicitly map structural relationships from source to target domain."""
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system="""Perform structural mapping between domains:
1. Identify the key objects, relationships, and constraints in the source domain
2. Find the corresponding objects, relationships, and constraints in the target problem
3. Identify which solution strategies transfer and which don't
4. Apply the transferred strategy to solve the target problem""",
        messages=[{"role": "user", "content": f"Source domain: {source_domain}\nTarget problem: {target_problem}\n\nPerform structural mapping and solve."}]
    )
    return resp.content[0].text

async def multi_domain_analogy(problem: str) -> dict:
    # Try multiple source domains in parallel
    source_domains = [
        "Water flow through pipes (fluid dynamics)",
        "Traffic flow through road networks",
        "Electricity through circuits (Ohm's law, Kirchhoff's laws)",
    ]
    solutions = await asyncio.gather(*[structure_map(domain, problem) for domain in source_domains])

    # Synthesize the best insights
    synthesis_input = "\n\n".join([f"**From {domain}:**\n{sol[:300]}" for domain, sol in zip(source_domains, solutions)])
    synth = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system="Synthesize the key insights from multiple analogical solutions into one coherent answer.",
        messages=[{"role": "user", "content": f"Problem: {problem}\n\nAnalogical solutions:\n{synthesis_input}"}]
    )
    return {
        "problem": problem,
        "domain_solutions": dict(zip(source_domains, [s[:200] for s in solutions])),
        "synthesis": synth.content[0].text
    }

async def main():
    problem = "How should we design the routing of requests through microservices to minimize latency while respecting capacity limits?"
    result = await multi_domain_analogy(problem)
    print(f"Synthesis:\n{result['synthesis'][:400]}")

asyncio.run(main())
```

---

## Option 4: Progressive Abstraction Ladder

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

async def abstract_problem(problem: str, level: int) -> str:
    """Abstract the problem to level N (0=concrete, 3=most abstract)."""
    level_descriptions = [
        "Keep all specific details. Express in original domain terms.",
        "Replace domain-specific terms with generic ones. Focus on the structure.",
        "Express as a pure mathematical/logical problem. Remove all domain context.",
        "Express as the most fundamental principle or pattern underlying this problem.",
    ]
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=f"Abstraction level {level}: {level_descriptions[level]}",
        messages=[{"role": "user", "content": f"Restate this problem at the requested abstraction level:\n{problem}"}]
    )
    return resp.content[0].text

async def find_solutions_at_abstract_level(abstract_problem: str) -> str:
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        system="At this abstract level, what known problems share this structure? What are their solutions?",
        messages=[{"role": "user", "content": abstract_problem}]
    )
    return resp.content[0].text

async def abstraction_ladder_solve(problem: str) -> dict:
    # Create abstraction ladder
    abstractions = await asyncio.gather(*[abstract_problem(problem, level) for level in range(4)])

    # Find solutions at most abstract level
    abstract_solutions = await find_solutions_at_abstract_level(abstractions[2])

    # Map abstract solutions back to concrete domain
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system="Map these abstract solution patterns back to the specific domain of the original problem.",
        messages=[{"role": "user", "content": f"Original problem:\n{problem}\n\nAbstract form:\n{abstractions[2]}\n\nAbstract solutions:\n{abstract_solutions}\n\nApply to the original domain."}]
    )
    return {
        "concrete": abstractions[0],
        "abstract": abstractions[2],
        "most_abstract": abstractions[3],
        "abstract_solutions": abstract_solutions,
        "concrete_solution": resp.content[0].text
    }

async def main():
    problem = "How do we fairly allocate limited hospital beds among patients with different urgency levels and expected recovery times?"
    result = await abstraction_ladder_solve(problem)
    print(f"Abstract form: {result['abstract'][:200]}")
    print(f"Solution: {result['concrete_solution'][:300]}")

asyncio.run(main())
```

---

## Option 5: Analogical Decomposition for Sub-Problems

```python
import asyncio
import anthropic
import json
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class SubProblem:
    description: str
    analogy: str = ""
    solution: str = ""

async def decompose_into_sub_problems(problem: str) -> list[SubProblem]:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system='Decompose into 3-4 independent sub-problems. Return JSON: [{"description": "..."}]',
        messages=[{"role": "user", "content": problem}]
    )
    try:
        data = json.loads(resp.content[0].text)
        return [SubProblem(**item) for item in data]
    except Exception:
        return [SubProblem(description=problem)]

async def find_analogy_for_sub(sub: SubProblem) -> SubProblem:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system="Name one well-known analogous problem from any domain and state its key solution insight in 2 sentences.",
        messages=[{"role": "user", "content": sub.description}]
    )
    sub.analogy = resp.content[0].text
    return sub

async def solve_sub_with_analogy(sub: SubProblem) -> SubProblem:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=f"Use this analogy to guide your solution:\n{sub.analogy}",
        messages=[{"role": "user", "content": sub.description}]
    )
    sub.solution = resp.content[0].text
    return sub

async def synthesize(problem: str, subs: list[SubProblem]) -> str:
    parts = "\n\n".join([f"[Sub-problem: {s.description[:60]}]\nAnalogy: {s.analogy[:100]}\nSolution: {s.solution[:150]}" for s in subs])
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system="Synthesize sub-problem solutions into a coherent overall solution.",
        messages=[{"role": "user", "content": f"Problem: {problem}\n\nSub-solutions:\n{parts}"}]
    )
    return resp.content[0].text

async def analogical_decomposition(problem: str) -> str:
    subs = await decompose_into_sub_problems(problem)
    subs = await asyncio.gather(*[find_analogy_for_sub(s) for s in subs])
    subs = await asyncio.gather(*[solve_sub_with_analogy(s) for s in subs])
    return await synthesize(problem, list(subs))

async def main():
    problem = "Design a system that learns user preferences over time and provides personalized recommendations while respecting privacy."
    solution = await analogical_decomposition(problem)
    print(f"Solution:\n{solution[:400]}")

asyncio.run(main())
```

---

## Option 6: Contrastive Analogies (What Works and What Doesn't)

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

async def generate_positive_analogy(problem: str) -> tuple[str, str]:
    """Find an analogy where the same approach works well."""
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system="Find a domain where a similar problem is solved successfully. Describe the solution and why it works.",
        messages=[{"role": "user", "content": problem}]
    )
    return "positive", resp.content[0].text

async def generate_negative_analogy(problem: str) -> tuple[str, str]:
    """Find an analogy where a naive approach fails — and why."""
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system="Find a domain where a naive solution to a similar problem fails. Describe the failure and what was learned from it.",
        messages=[{"role": "user", "content": problem}]
    )
    return "negative", resp.content[0].text

async def contrastive_analogy_solve(problem: str) -> dict:
    # Run positive and negative analogies in parallel
    (pos_type, pos_analogy), (neg_type, neg_analogy) = await asyncio.gather(
        generate_positive_analogy(problem),
        generate_negative_analogy(problem)
    )

    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=700,
        system="""Use contrastive analogies to design a robust solution:
- Learn from what worked in the positive analogy
- Avoid the failure modes revealed in the negative analogy
- Explicitly address the tension between them""",
        messages=[{"role": "user", "content": f"Problem: {problem}\n\nSuccessful analogy:\n{pos_analogy}\n\nFailed analogy (warning):\n{neg_analogy}\n\nDesign a solution that captures the success and avoids the failure."}]
    )
    return {
        "positive_analogy": pos_analogy,
        "negative_analogy": neg_analogy,
        "solution": resp.content[0].text
    }

async def main():
    problem = "How should an AI agent handle uncertainty when it doesn't know whether to trust a user's claim about their identity?"
    result = await contrastive_analogy_solve(problem)
    print(f"Positive analogy: {result['positive_analogy'][:200]}\n")
    print(f"Negative analogy: {result['negative_analogy'][:200]}\n")
    print(f"Solution: {result['solution'][:300]}")

asyncio.run(main())
```

---

## Comparison

| Option | Analogy Source | Parallelism | Best For |
|--------|--------------|-------------|----------|
| 1 – Self-Generated | LLM generates on-the-fly | None | General novel problems |
| 2 – Retrieved Library | Pre-curated problem DB | None | Domain-specific agents |
| 3 – Multi-Domain Mapping | 3 parallel domains | Yes (3 domains) | Complex structural problems |
| 4 – Abstraction Ladder | LLM abstracts progressively | Partial | Mathematical/logical problems |
| 5 – Analogical Decomposition | Per sub-problem analogy | Yes (sub-problems) | Multi-faceted problems |
| 6 – Contrastive | Positive + negative analogies | Yes (2 analogies) | Design problems with tradeoffs |

**Recommendation:** Use Option 1 for general-purpose agents — self-generated analogies add minimal cost (one extra Haiku call) and measurably improve reasoning quality on novel problems. Use Option 2 when your domain has a curated set of canonical problems with known solutions. Use Option 6 for design and architecture problems where understanding failure modes is as important as understanding successes.
