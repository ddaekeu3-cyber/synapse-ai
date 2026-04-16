---
title: "Agent Doesn't Implement Step-Back Prompting for Abstract Reasoning"
description: "How to use step-back prompting—asking the model to reason from first principles before answering—to improve accuracy on complex, multi-step reasoning tasks."
categories: [prompt-engineering]
difficulty: intermediate
---

Step-back prompting asks the model to first identify the underlying principles or abstractions relevant to a question before attempting to answer it directly. This two-stage process reduces errors caused by jumping straight to a specific conclusion without grounding the reasoning in general knowledge.

## Solution 1: Basic Two-Stage Step-Back

Explicitly ask for the abstract principle first, then use it to answer the specific question.

```python
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-sonnet-4-6"


async def step_back_answer(question: str) -> dict:
    # Stage 1: Step back to general principles
    stepback_resp = await client.messages.create(
        model=MODEL,
        max_tokens=300,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Before answering the following question, first identify the general "
                    f"concepts, principles, or domain knowledge that are most relevant to it. "
                    f"Do not answer the question yet.\n\nQuestion: {question}"
                ),
            }
        ],
    )
    principles = stepback_resp.content[0].text

    # Stage 2: Use the principles to answer
    answer_resp = await client.messages.create(
        model=MODEL,
        max_tokens=500,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Relevant principles:\n{principles}\n\n"
                    f"Now use these principles to answer the original question accurately:\n{question}"
                ),
            }
        ],
    )

    return {
        "question": question,
        "principles": principles,
        "answer": answer_resp.content[0].text,
    }


async def main():
    question = (
        "If a ball is dropped from 45 meters and loses 20% of its energy on each bounce, "
        "how high will it reach after the third bounce?"
    )
    result = await step_back_answer(question)
    print(f"Principles:\n{result['principles']}\n")
    print(f"Answer:\n{result['answer']}")


import asyncio
asyncio.run(main())
```

## Solution 2: Domain-Aware Step-Back with Classifier

Detect the question domain first, then apply domain-specific step-back prompts tailored to that area.

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-sonnet-4-6"

DOMAIN_STEPBACK_PROMPTS = {
    "physics": "Identify the physics laws, formulas, and conservation principles that apply.",
    "math": "Identify the mathematical theorems, definitions, and proof techniques needed.",
    "history": "Identify the historical context, causation patterns, and relevant time period dynamics.",
    "biology": "Identify the biological mechanisms, evolutionary pressures, and systems involved.",
    "logic": "Identify the logical structure, quantifiers, and inference rules at play.",
    "default": "Identify the key concepts, definitions, and general principles relevant to this question.",
}


async def classify_domain(question: str) -> str:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Classify this question into one domain: physics, math, history, biology, logic, or other.\n"
                    f"Reply with only the domain name.\n\nQuestion: {question}"
                ),
            }
        ],
    )
    domain = resp.content[0].text.strip().lower()
    return domain if domain in DOMAIN_STEPBACK_PROMPTS else "default"


async def domain_step_back(question: str) -> str:
    domain = await classify_domain(question)
    stepback_instruction = DOMAIN_STEPBACK_PROMPTS[domain]

    # Step 1: Elicit domain-specific principles
    principles_resp = await client.messages.create(
        model=MODEL,
        max_tokens=400,
        messages=[
            {
                "role": "user",
                "content": f"{stepback_instruction}\n\nQuestion: {question}",
            }
        ],
    )
    principles = principles_resp.content[0].text

    # Step 2: Answer grounded in those principles
    answer_resp = await client.messages.create(
        model=MODEL,
        max_tokens=600,
        messages=[
            {"role": "user", "content": f"{stepback_instruction}\n\nQuestion: {question}"},
            {"role": "assistant", "content": principles},
            {"role": "user", "content": f"Now answer the question using these principles: {question}"},
        ],
    )

    return f"[Domain: {domain}]\n\nPrinciples:\n{principles}\n\nAnswer:\n{answer_resp.content[0].text}"


async def main():
    questions = [
        "Why does a gyroscope resist tilting when spinning?",
        "Prove that the square root of 2 is irrational.",
    ]
    results = await asyncio.gather(*[domain_step_back(q) for q in questions])
    for q, r in zip(questions, results):
        print(f"Q: {q}\n{r}\n{'='*60}\n")


asyncio.run(main())
```

## Solution 3: Multi-Hop Step-Back Chain

For deeply complex questions, apply step-back recursively: abstract from the specific → sub-principle → general principle.

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-sonnet-4-6"


async def single_stepback(context: str, question: str, level: int) -> str:
    level_prompts = {
        1: "What are the specific sub-concepts and intermediate rules needed to answer this?",
        2: "What are the broader principles and theoretical foundations that govern those sub-concepts?",
        3: "What are the most fundamental axioms or laws that underpin everything above?",
    }
    instruction = level_prompts.get(level, level_prompts[3])

    resp = await client.messages.create(
        model=MODEL,
        max_tokens=300,
        messages=[
            {
                "role": "user",
                "content": (
                    f"{instruction}\n\n"
                    f"{'Previous context: ' + context + chr(10) + chr(10) if context else ''}"
                    f"Question: {question}"
                ),
            }
        ],
    )
    return resp.content[0].text


async def multi_hop_step_back(question: str, hops: int = 3) -> str:
    layers = []
    context = ""

    # Build abstraction layers
    for level in range(hops, 0, -1):
        abstraction = await single_stepback(context, question, level)
        layers.append((level, abstraction))
        context = abstraction

    # Final answer using all layers
    layered_context = "\n\n".join(
        f"[Level {level} abstraction]\n{text}" for level, text in reversed(layers)
    )

    answer_resp = await client.messages.create(
        model=MODEL,
        max_tokens=700,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Using the following layered reasoning context, "
                    f"answer the question precisely:\n\n"
                    f"{layered_context}\n\n"
                    f"Question: {question}"
                ),
            }
        ],
    )

    return answer_resp.content[0].text


async def main():
    question = (
        "Why does entropy always increase in an isolated system, "
        "and what does this imply about the arrow of time?"
    )
    answer = await multi_hop_step_back(question, hops=3)
    print(f"Q: {question}\n\nA: {answer}")


asyncio.run(main())
```

## Solution 4: Step-Back with Socratic Self-Questioning

Generate a set of clarifying sub-questions via step-back, answer each, then synthesize.

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-sonnet-4-6"


async def generate_subquestions(question: str) -> list[str]:
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=300,
        messages=[
            {
                "role": "user",
                "content": (
                    f"To answer the following question well, what 3-5 simpler prerequisite questions "
                    f"should be answered first? List them one per line, no numbering.\n\n"
                    f"Question: {question}"
                ),
            }
        ],
    )
    lines = [l.strip() for l in resp.content[0].text.strip().splitlines() if l.strip()]
    return lines[:5]


async def answer_subquestion(sub_q: str) -> str:
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=200,
        messages=[{"role": "user", "content": sub_q}],
    )
    return resp.content[0].text


async def socratic_step_back(question: str) -> str:
    subquestions = await generate_subquestions(question)

    # Answer all subquestions in parallel
    answers = await asyncio.gather(*[answer_subquestion(sq) for sq in subquestions])

    sub_qa_pairs = "\n\n".join(
        f"Q: {sq}\nA: {ans}" for sq, ans in zip(subquestions, answers)
    )

    # Synthesize final answer
    synthesis_resp = await client.messages.create(
        model=MODEL,
        max_tokens=600,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Using the following prerequisite answers as grounding:\n\n"
                    f"{sub_qa_pairs}\n\n"
                    f"Now answer the original question:\n{question}"
                ),
            }
        ],
    )
    return synthesis_resp.content[0].text


async def main():
    question = "How does compound interest lead to wealth inequality over generations?"
    answer = await socratic_step_back(question)
    print(f"Q: {question}\n\nA: {answer}")


asyncio.run(main())
```

## Solution 5: Comparison Step-Back (Contrast with Baseline)

Have the model answer directly first, then step back to identify weaknesses, then produce an improved answer.

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-sonnet-4-6"


async def comparison_step_back(question: str) -> dict:
    # Phase 1: Direct answer (baseline)
    direct_resp = await client.messages.create(
        model=MODEL,
        max_tokens=400,
        messages=[{"role": "user", "content": question}],
    )
    direct_answer = direct_resp.content[0].text

    # Phase 2: Step back — critique the direct answer
    critique_resp = await client.messages.create(
        model=MODEL,
        max_tokens=300,
        messages=[
            {"role": "user", "content": question},
            {"role": "assistant", "content": direct_answer},
            {
                "role": "user",
                "content": (
                    "Step back and critically evaluate your answer above. "
                    "What important principles, edge cases, or nuances did you miss? "
                    "What assumptions did you make that might not hold?"
                ),
            },
        ],
    )
    critique = critique_resp.content[0].text

    # Phase 3: Improved answer informed by the critique
    improved_resp = await client.messages.create(
        model=MODEL,
        max_tokens=600,
        messages=[
            {"role": "user", "content": question},
            {"role": "assistant", "content": direct_answer},
            {
                "role": "user",
                "content": (
                    "Step back and critically evaluate your answer above. "
                    "What important principles, edge cases, or nuances did you miss? "
                    "What assumptions did you make that might not hold?"
                ),
            },
            {"role": "assistant", "content": critique},
            {
                "role": "user",
                "content": "Now provide an improved, more complete answer incorporating your self-critique.",
            },
        ],
    )

    return {
        "question": question,
        "direct": direct_answer,
        "critique": critique,
        "improved": improved_resp.content[0].text,
    }


async def main():
    question = "What are the trade-offs of using microservices vs a monolith architecture?"
    result = await comparison_step_back(question)
    print(f"Direct answer:\n{result['direct']}\n")
    print(f"Critique:\n{result['critique']}\n")
    print(f"Improved answer:\n{result['improved']}")


asyncio.run(main())
```

## Solution 6: Batch Step-Back with Parallel Principle Retrieval

For pipelines that process many questions, apply step-back in parallel and cache shared principles.

```python
import asyncio
import hashlib
from functools import lru_cache
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-sonnet-4-6"

# Simple in-process principle cache (keyed by question hash)
_principle_cache: dict[str, str] = {}


def cache_key(question: str) -> str:
    return hashlib.md5(question.encode()).hexdigest()


async def get_principles(question: str) -> str:
    key = cache_key(question)
    if key in _principle_cache:
        return _principle_cache[key]

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",  # Cheaper model for principle extraction
        max_tokens=250,
        messages=[
            {
                "role": "user",
                "content": (
                    "List the core principles and concepts needed to reason about "
                    f"this question correctly (2-4 bullet points):\n\n{question}"
                ),
            }
        ],
    )
    principles = resp.content[0].text
    _principle_cache[key] = principles
    return principles


async def step_back_single(question: str) -> str:
    principles = await get_principles(question)

    resp = await client.messages.create(
        model=MODEL,
        max_tokens=400,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Core principles:\n{principles}\n\n"
                    f"Answer using these principles:\n{question}"
                ),
            }
        ],
    )
    return resp.content[0].text


async def batch_step_back(questions: list[str]) -> list[str]:
    return list(await asyncio.gather(*[step_back_single(q) for q in questions]))


async def main():
    questions = [
        "Why do bridges use triangular trusses?",
        "How does the Doppler effect work?",
        "What causes stock market bubbles?",
        "Why is recursion sometimes preferable to iteration?",
    ]

    answers = await batch_step_back(questions)
    for q, a in zip(questions, answers):
        print(f"Q: {q}\nA: {a[:200]}…\n")

    print(f"Principle cache size: {len(_principle_cache)} entries")


asyncio.run(main())
```

## Comparison

| Solution | Extra API calls | Latency | Accuracy gain | Best for |
|---|---|---|---|---|
| **Basic two-stage** | +1 | Low | Moderate | General-purpose improvement |
| **Domain-aware** | +2 (classify + abstract) | Medium | High | Domain-specific question sets |
| **Multi-hop chain** | +N hops | High | Highest | Deep, multi-layered problems |
| **Socratic sub-questions** | +1 + N parallel | Medium | High | Open-ended reasoning |
| **Comparison critique** | +2 | Medium | High | Self-improving pipelines |
| **Batch parallel** | +1 (Haiku) | Low (parallel) | Moderate | High-volume question processing |

Start with **basic two-stage** (Solution 1) — one extra Haiku call for meaningful accuracy gains. Upgrade to **socratic sub-questions** (Solution 4) for complex reasoning chains. Use **batch parallel** (Solution 6) to minimize latency when processing many questions simultaneously.
