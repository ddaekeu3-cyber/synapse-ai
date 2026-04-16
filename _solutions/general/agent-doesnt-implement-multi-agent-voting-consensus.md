---
layout: solution
title: "Agent Doesn't Implement Multi-Agent Voting Consensus"
category: general
description: "Run the same prompt across multiple agent instances and aggregate their answers via majority vote, weighted scoring, or LLM synthesis to improve reliability."
tags: [general, multi-agent, voting, consensus, reliability, ensemble]
---

# Agent Doesn't Implement Multi-Agent Voting Consensus

A single agent response can be confidently wrong. Hallucinations, prompt sensitivity, and model stochasticity mean any single run may produce an incorrect answer that looks authoritative. Running the same question through multiple independent agents and aggregating their answers via voting or synthesis catches individual failures and produces more reliable output — the ensemble is more accurate than any single member.

## Option 1: Majority Vote on Categorical Answers

```python
import asyncio
import anthropic
from collections import Counter

client = anthropic.AsyncAnthropic()

N_AGENTS = 5  # Number of independent votes


async def single_agent_vote(question: str, agent_id: int) -> str:
    """One agent produces one answer — categorical (e.g., yes/no, A/B/C)."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=32,
        messages=[{
            "role": "user",
            "content": f"{question}\n\nRespond with only the answer (one word or short phrase). No explanation."
        }],
    )
    answer = response.content[0].text.strip().lower()
    print(f"  Agent {agent_id}: {answer!r}")
    return answer


async def majority_vote(question: str, n: int = N_AGENTS) -> tuple[str, float]:
    """
    Run n agents in parallel and return the majority answer with confidence.
    """
    tasks = [asyncio.create_task(single_agent_vote(question, i)) for i in range(1, n + 1)]
    votes: list[str] = await asyncio.gather(*tasks)

    counts = Counter(votes)
    winner, count = counts.most_common(1)[0]
    confidence = count / n

    print(f"\nVotes: {dict(counts)}")
    print(f"Winner: {winner!r} ({count}/{n} = {confidence:.0%} confidence)")
    return winner, confidence


async def main() -> None:
    questions = [
        "Is Python a statically typed language? Answer: yes or no.",
        "Which is faster for CPU-bound tasks: threading or multiprocessing? Answer: threading or multiprocessing.",
        "Is 127 a prime number? Answer: yes or no.",
    ]
    for q in questions:
        print(f"\nQ: {q}")
        answer, confidence = await majority_vote(q, n=5)
        print(f"Consensus: {answer} (confidence={confidence:.0%})")


asyncio.run(main())

# Expected Token Savings: N/A (accuracy pattern); 5 agents cost 5x tokens but reduce categorical error rate significantly
# Environment: Python 3.11+; use n=3 for 2x cost with majority; n=5 for 2.5x cost with stronger consensus
```

## Option 2: Weighted Confidence Voting

```python
import asyncio
import anthropic
import json
from collections import defaultdict

client = anthropic.AsyncAnthropic()


async def agent_with_confidence(question: str, agent_id: int) -> tuple[str, float]:
    """Agent returns answer + self-reported confidence score."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{
            "role": "user",
            "content": (
                f"{question}\n\n"
                "Respond with JSON only: "
                '{"answer": "<your answer>", "confidence": <0.0-1.0>}'
            )
        }],
    )
    raw = response.content[0].text.strip()
    try:
        data = json.loads(raw)
        answer = str(data.get("answer", "")).strip().lower()
        confidence = float(data.get("confidence", 0.5))
        return answer, max(0.0, min(1.0, confidence))
    except Exception:
        # Fallback: extract first word
        return raw.split()[0].lower() if raw else "unknown", 0.3


async def weighted_vote(question: str, n: int = 5) -> tuple[str, float]:
    """
    Each agent casts a confidence-weighted vote.
    Winner = answer with highest total confidence weight.
    """
    tasks = [asyncio.create_task(agent_with_confidence(question, i)) for i in range(1, n + 1)]
    results: list[tuple[str, float]] = await asyncio.gather(*tasks)

    weights: dict[str, float] = defaultdict(float)
    for answer, confidence in results:
        weights[answer] += confidence
        print(f"  Agent voted: {answer!r} (confidence={confidence:.2f})")

    total_weight = sum(weights.values())
    winner = max(weights, key=weights.__getitem__)
    winner_weight = weights[winner]
    normalized_confidence = winner_weight / total_weight if total_weight > 0 else 0.0

    print(f"\nWeighted votes: {dict(weights)}")
    print(f"Winner: {winner!r} (weight={winner_weight:.2f}, normalized={normalized_confidence:.0%})")
    return winner, normalized_confidence


async def main() -> None:
    questions = [
        "What is the time complexity of binary search? (e.g., O(n), O(log n), O(n^2))",
        "Is asyncio in Python cooperative or preemptive multitasking?",
    ]
    for q in questions:
        print(f"\nQ: {q}")
        answer, conf = await weighted_vote(q, n=5)
        print(f"Consensus: {answer} (confidence={conf:.0%})")


asyncio.run(main())

# Expected Token Savings: N/A; weighted voting down-weights uncertain agents, improving consensus accuracy
# Environment: Python 3.11+; self-reported confidence is imperfect — calibrate against ground truth if possible
```

## Option 3: LLM-Synthesized Consensus from Diverse Responses

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

# Use slightly different system prompts to encourage diverse perspectives
AGENT_PERSONAS = [
    "You are a precise technical expert. Be exact and cite specifics.",
    "You are a pragmatic engineer. Focus on real-world implications.",
    "You are a cautious reviewer. Highlight edge cases and caveats.",
]


async def agent_response(question: str, persona: str, agent_id: int) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=persona,
        messages=[{"role": "user", "content": question}],
    )
    answer = response.content[0].text.strip()
    print(f"\n[Agent {agent_id}]\n{answer[:150]}")
    return answer


async def synthesize(question: str, responses: list[str]) -> str:
    """LLM synthesizes the best consensus from multiple agent responses."""
    formatted = "\n\n".join(f"Agent {i+1}:\n{r}" for i, r in enumerate(responses))
    synthesis_prompt = (
        f"Question: {question}\n\n"
        f"Multiple expert agents answered this question:\n\n{formatted}\n\n"
        "Synthesize the best, most accurate answer by combining correct elements and "
        "noting any disagreements. Be concise."
    )
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": synthesis_prompt}],
    )
    return response.content[0].text.strip()


async def consensus_with_synthesis(question: str) -> str:
    # Phase 1: Collect diverse responses in parallel
    tasks = [
        asyncio.create_task(agent_response(question, persona, i + 1))
        for i, persona in enumerate(AGENT_PERSONAS)
    ]
    responses = await asyncio.gather(*tasks)

    # Phase 2: Synthesize
    print("\n\n[SYNTHESIS]")
    result = await synthesize(question, list(responses))
    print(result)
    return result


async def main() -> None:
    question = "What are the trade-offs between using async/await vs threading in Python?"
    await consensus_with_synthesis(question)


asyncio.run(main())

# Expected Token Savings: N/A; synthesis adds ~200 tokens but produces better answer than any single agent alone
# Environment: Python 3.11+; use different temperatures per agent for more diversity: not configurable in current SDK
```

## Option 4: Self-Consistency Sampling with Canonical Answer Extraction

```python
import asyncio
import anthropic
import re
from collections import Counter

client = anthropic.AsyncAnthropic()

N_SAMPLES = 5


async def sample_response(question: str, sample_id: int) -> str:
    """Sample one response with chain-of-thought reasoning."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"{question}\n\nReason step by step, then state your final answer on a new line starting with 'ANSWER:'"
        }],
    )
    return response.content[0].text.strip()


def extract_answer(text: str) -> str:
    """Extract the final answer from a chain-of-thought response."""
    # Look for explicit ANSWER: marker
    match = re.search(r"ANSWER:\s*(.+?)(?:\n|$)", text, re.IGNORECASE)
    if match:
        return match.group(1).strip().lower()
    # Fallback: last sentence
    sentences = [s.strip() for s in text.split(".") if s.strip()]
    return sentences[-1].lower() if sentences else text[:50].lower()


async def self_consistency(question: str, n: int = N_SAMPLES) -> tuple[str, float, list[str]]:
    """
    Self-consistency: sample N reasoning chains, extract each answer,
    return the most common answer with its consistency score.
    """
    tasks = [asyncio.create_task(sample_response(question, i)) for i in range(1, n + 1)]
    full_responses = await asyncio.gather(*tasks)

    answers = [extract_answer(r) for r in full_responses]

    for i, (resp, ans) in enumerate(zip(full_responses, answers)):
        print(f"\n[Sample {i+1}] Extracted: {ans!r}")
        print(f"  Reasoning: {resp[:100]}...")

    counts = Counter(answers)
    most_common, count = counts.most_common(1)[0]
    consistency = count / n

    return most_common, consistency, answers


async def main() -> None:
    questions = [
        "A store sells apples for $0.50 each. If you buy 7 apples and pay with a $5 bill, how much change do you get?",
        "Is the following valid Python: `x = lambda a, b: a if a > b else b`? (yes/no)",
    ]
    for q in questions:
        print(f"\n{'='*50}\nQ: {q}")
        answer, consistency, all_answers = await self_consistency(q, n=N_SAMPLES)
        print(f"\nConsensus: {answer!r} (consistency={consistency:.0%})")
        print(f"All answers: {all_answers}")


asyncio.run(main())

# Expected Token Savings: N/A; self-consistency (Wang et al. 2022) improves reasoning accuracy by 10-20% on math/logic
# Environment: Python 3.11+; works best on questions with deterministic correct answers; less useful for open-ended tasks
```

## Option 5: Debate Protocol Between Two Agents

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

MAX_DEBATE_ROUNDS = 2


async def initial_answer(question: str, agent_name: str, stance: str) -> str:
    """Agent gives its initial answer."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=f"You are {agent_name}. {stance} Be direct and specific.",
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text.strip()


async def critique_and_refine(question: str, agent_name: str, own_answer: str,
                               opponent_answer: str) -> str:
    """Agent critiques the opponent and refines its own position."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=f"You are {agent_name}. Identify flaws in the opposing argument and strengthen your own.",
        messages=[{
            "role": "user",
            "content": (
                f"Question: {question}\n\n"
                f"Your previous answer: {own_answer}\n\n"
                f"Opposing agent's answer: {opponent_answer}\n\n"
                "Critique the opposing answer and provide your refined position."
            )
        }],
    )
    return response.content[0].text.strip()


async def judge_debate(question: str, agent_a_final: str, agent_b_final: str) -> str:
    """Neutral judge selects the better-argued answer."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system="You are an impartial judge. Select the most accurate and well-reasoned answer.",
        messages=[{
            "role": "user",
            "content": (
                f"Question: {question}\n\n"
                f"Agent A: {agent_a_final}\n\n"
                f"Agent B: {agent_b_final}\n\n"
                "Which answer is more accurate and why? State the winner (A or B) and your reasoning."
            )
        }],
    )
    return response.content[0].text.strip()


async def debate_consensus(question: str) -> str:
    # Round 0: Initial answers
    print("[Round 0] Initial positions")
    a0, b0 = await asyncio.gather(
        initial_answer(question, "Agent A", "Present your best answer to the question."),
        initial_answer(question, "Agent B", "Present your best answer to the question."),
    )
    print(f"Agent A: {a0[:100]}")
    print(f"Agent B: {b0[:100]}")

    a_pos, b_pos = a0, b0

    # Debate rounds
    for round_num in range(1, MAX_DEBATE_ROUNDS + 1):
        print(f"\n[Round {round_num}] Critique and refine")
        a_pos, b_pos = await asyncio.gather(
            critique_and_refine(question, "Agent A", a_pos, b_pos),
            critique_and_refine(question, "Agent B", b_pos, a_pos),
        )
        print(f"Agent A: {a_pos[:100]}")
        print(f"Agent B: {b_pos[:100]}")

    # Judge
    print("\n[Judgment]")
    verdict = await judge_debate(question, a_pos, b_pos)
    print(verdict)
    return verdict


async def main() -> None:
    question = "Is it better to use a relational database or a document database for a social media application?"
    await debate_consensus(question)


asyncio.run(main())

# Expected Token Savings: N/A; debate protocol adds 4-6x tokens but surfaces nuanced trade-offs single agents miss
# Environment: Python 3.11+; 2 debate rounds is typically sufficient; more rounds show diminishing returns
```

## Option 6: Ensemble with Disagreement Detection and Escalation

```python
import asyncio
import anthropic
import difflib

client = anthropic.AsyncAnthropic()

DISAGREEMENT_THRESHOLD = 0.5   # Similarity below this = agents disagree
N_AGENTS = 3
N_ESCALATION_AGENTS = 2       # Extra agents added when disagreement is high


async def get_answer(question: str, agent_id: int, max_tokens: int = 256) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text.strip()


def pairwise_similarity(answers: list[str]) -> float:
    """Average pairwise similarity across all answer pairs."""
    if len(answers) < 2:
        return 1.0
    pairs = [(answers[i], answers[j]) for i in range(len(answers)) for j in range(i+1, len(answers))]
    sims = [difflib.SequenceMatcher(None, a, b).ratio() for a, b in pairs]
    return sum(sims) / len(sims)


async def synthesize_final(question: str, answers: list[str]) -> str:
    """Synthesize final answer from all collected answers."""
    combined = "\n\n".join(f"Answer {i+1}: {a}" for i, a in enumerate(answers))
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": f"Question: {question}\n\nMultiple answers:\n{combined}\n\nSynthesize the most accurate answer."
        }],
    )
    return response.content[0].text.strip()


async def ensemble_with_escalation(question: str) -> tuple[str, dict]:
    """Run ensemble, escalate with more agents if disagreement is high."""
    stats: dict = {"initial_agents": N_AGENTS, "escalated": False, "total_agents": N_AGENTS}

    # Initial ensemble
    tasks = [asyncio.create_task(get_answer(question, i)) for i in range(1, N_AGENTS + 1)]
    answers: list[str] = await asyncio.gather(*tasks)

    sim = pairwise_similarity(answers)
    print(f"[initial] {N_AGENTS} agents | avg similarity={sim:.2f}")
    for i, a in enumerate(answers):
        print(f"  Agent {i+1}: {a[:80]}")

    # Escalate if disagreement is high
    if sim < DISAGREEMENT_THRESHOLD:
        print(f"\n[escalating] Low agreement ({sim:.2f} < {DISAGREEMENT_THRESHOLD}). Adding {N_ESCALATION_AGENTS} more agents...")
        extra_tasks = [
            asyncio.create_task(get_answer(question, N_AGENTS + i + 1))
            for i in range(N_ESCALATION_AGENTS)
        ]
        extra_answers = await asyncio.gather(*extra_tasks)
        answers.extend(extra_answers)
        stats["escalated"] = True
        stats["total_agents"] = len(answers)

        new_sim = pairwise_similarity(answers)
        print(f"[escalated] {len(answers)} agents | new similarity={new_sim:.2f}")

    # Final synthesis
    print("\n[synthesis]")
    final = await synthesize_final(question, answers)
    print(final[:300])

    return final, stats


async def main() -> None:
    questions = [
        "Should Python use tabs or spaces? Give a definitive recommendation.",
        "What is 15 * 17?",
    ]
    for q in questions:
        print(f"\n{'='*50}\nQ: {q}")
        answer, stats = await ensemble_with_escalation(q)
        print(f"\nStats: {stats}")


asyncio.run(main())

# Expected Token Savings: Escalation only triggers on disagreement — saves extra agents on clear-cut questions
# Environment: Python 3.11+; tune DISAGREEMENT_THRESHOLD (0.4-0.6) based on acceptable answer variance for your domain
```

## Comparison

| Option | Aggregation Method | Open-Ended | Reasoning | Escalation | Best For |
|--------|-------------------|------------|-----------|------------|----------|
| 1. Majority Vote | Mode of categorical answers | No | No | No | Classification, yes/no, multiple choice |
| 2. Weighted Confidence | Confidence-weighted vote | No | No | No | Categorical with self-assessed certainty |
| 3. LLM Synthesis | Synthesizer agent | Yes | No | No | Open-ended explanatory questions |
| 4. Self-Consistency | Most frequent extracted answer | Partial | Yes (CoT) | No | Math, logic, structured reasoning |
| 5. Debate Protocol | Judge between adversarial agents | Yes | Yes | No | Trade-off analysis, nuanced decisions |
| 6. Ensemble + Escalate | Synthesis with disagreement gate | Yes | No | Yes | Variable-confidence questions |
