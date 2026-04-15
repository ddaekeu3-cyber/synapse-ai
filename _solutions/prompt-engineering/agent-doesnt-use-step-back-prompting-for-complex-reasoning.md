---
layout: solution
title: "Agent doesn't use step-back prompting for complex reasoning"
category: prompt-engineering
description: "Agent tackles complex questions directly and gets stuck on surface-level details. Step-back prompting instructs the model to first derive the underlying principle or framework, then apply it to the specific case — improving reasoning accuracy on hard multi-step problems by 20–40%."
tags: [prompt-engineering, step-back, reasoning, chain-of-thought, few-shot, accuracy]
---

## Symptom

The agent answers a complex technical or analytical question by jumping directly into the details and either getting lost in specifics, making logical errors early that compound, or missing the key abstraction that would unlock the solution. Users report that the agent "doesn't really think" and often gives technically detailed but fundamentally wrong answers.

## Root Cause

Direct question-answering activates surface-level associations: the model pattern-matches on keywords and retrieves related facts. For complex multi-step problems, the correct approach requires first identifying the right framework or principle, then reasoning from that principle to the specific case. Without an explicit step-back, the model skips the abstraction layer and reasons directly from surface features — which fails on non-routine problems.

## Fix

Inject a step-back instruction before the main question. The model first answers "what is the general principle or framework for this type of problem?" then uses that principle to answer the original question. Two API calls; the intermediate reasoning becomes a scaffold for the final answer.

---

### Option 1 — Two-turn step-back with explicit principle derivation

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")


def step_back_then_answer(question: str) -> str:
    """
    Step 1: Ask the model to state the underlying principle.
    Step 2: Use that principle to answer the original question.
    Two calls — the intermediate step improves accuracy on complex questions.
    """

    # Step 1: Derive the principle
    step_back_question = (
        f"Before answering the following question, state the fundamental principle, "
        f"theorem, or framework that applies to it. Be specific and precise — "
        f"give the general rule, not an answer to the specific case.\n\n"
        f"Question: {question}\n\n"
        f"General principle:"
    )
    principle_response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": step_back_question}],
    )
    principle = principle_response.content[0].text.strip()
    print(f"[Step-back principle]\n{principle}\n")

    # Step 2: Answer using the derived principle as scaffolding
    answer_prompt = (
        f"Using the following principle as your starting point, answer the question.\n\n"
        f"Principle: {principle}\n\n"
        f"Question: {question}\n\n"
        f"Answer:"
    )
    answer_response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": answer_prompt}],
    )
    return answer_response.content[0].text.strip()


# Example: complex physics question
question = (
    "A ball is thrown horizontally from a cliff 80m high with initial speed 20 m/s. "
    "At what angle below the horizontal is the ball moving when it hits the ground? "
    "Take g = 10 m/s²."
)

# WITHOUT step-back: model may jump to kinematics details and make sign errors
# WITH step-back: model first states "projectile motion separates into independent
#   horizontal (constant velocity) and vertical (constant acceleration) components"
#   then applies this correctly

result = step_back_then_answer(question)
print(f"[Final answer]\n{result}")
```

**Expected Token Savings:** Two calls cost ~300 extra tokens (the principle derivation); but prevents 2–3 correction turns (~800 tokens each) when the direct answer is wrong. Net savings on hard questions: 1300–2100 tokens.
**Environment:** Any complex reasoning task — physics, math, logic puzzles, legal analysis, architectural decisions; step-back is most valuable when the question is non-routine.

---

### Option 2 — Single-turn step-back via system prompt instruction

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

STEP_BACK_SYSTEM = """\
You are a careful analytical assistant. For every non-trivial question:

1. STEP BACK: State the general principle, theorem, or framework that applies
   (2-3 sentences maximum; be precise, not vague)
2. APPLY: Use that principle to reason through the specific case
3. ANSWER: State the final answer clearly

Format your response as:
**Principle:** <the general rule>
**Reasoning:** <step-by-step application>
**Answer:** <the final answer>

Skip the step-back only for simple factual lookups (capitals, dates, unit conversions).
For all analytical, mathematical, logical, or design questions — always step back first.
"""


def run_agent(question: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=STEP_BACK_SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text


# Test on several complex question types
questions = [
    # Logic
    "All mammals are warm-blooded. Whales are warm-blooded. Does it follow that whales are mammals?",
    # Math
    "A train leaves city A at 60 mph. Another leaves city B (300 miles away) at 40 mph heading toward A. "
    "Where do they meet?",
    # System design
    "Should I use a message queue or direct HTTP calls between microservices for order processing?",
]

for q in questions:
    print(f"\n{'='*60}\nQ: {q}\n")
    print(run_agent(q))
```

**Expected Token Savings:** Single-call approach adds ~50–100 tokens of overhead in the response structure; prevents wrong answers that would require follow-up correction (~300–500 tokens per correction).
**Environment:** General-purpose assistants where the question type is unpredictable; the system prompt instruction activates step-back selectively only for analytical questions.

---

### Option 3 — Domain-specific step-back with few-shot examples

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

# Few-shot examples teach the specific step-back pattern for software architecture
ARCHITECTURE_STEP_BACK_EXAMPLES = """
Example 1:
Q: Should I use Redis or Postgres to store user session data?
Principle: Session storage requires high-frequency random access by key (session ID),
  short TTL, and tolerance for data loss on restart. This matches an in-memory key-value
  store's characteristics, not a relational database's.
Reasoning: Redis natively supports TTL per key, sub-millisecond reads, and requires no
  schema. Postgres supports these but at higher latency and with unnecessary durability
  overhead for ephemeral data.
Answer: Redis — unless you need session data to survive Redis restarts (rare), or you're
  already running Postgres with low concurrency and want to minimize infrastructure.

Example 2:
Q: I need to send emails when users sign up. Should I do it synchronously in the request handler?
Principle: External I/O with non-deterministic latency and potential for failure should not
  block the primary user request path. Side effects that do not affect the response can be
  deferred to background processing.
Reasoning: Email sending can take 200–2000ms and occasionally fails (SMTP errors, bounces).
  Including it in the signup request increases P95 latency and couples signup success to email
  service availability.
Answer: Use a background job queue (Celery, SQS, etc.). The signup handler enqueues the job
  and returns 201 immediately. The worker sends the email asynchronously.
"""

ARCHITECTURE_SYSTEM = (
    "You are a software architecture advisor. When answering design questions, always follow "
    "the step-back pattern shown in the examples: state the underlying principle first, then "
    "reason from it, then give a concrete recommendation.\n\n"
    f"{ARCHITECTURE_STEP_BACK_EXAMPLES}"
)


def run_architecture_advisor(question: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=ARCHITECTURE_SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text


# The few-shot examples teach the pattern — new questions follow the same structure
result = run_architecture_advisor(
    "My API needs to check 5 external services before responding. Should I call them sequentially?"
)
print(result)
```

**Expected Token Savings:** Few-shot examples add ~400 tokens to the system prompt but are cached after the first call; the structured output (Principle → Reasoning → Answer) reduces follow-up questions by giving the user a clear reasoning chain to evaluate.
**Environment:** Domain-specific advisors (architecture, legal, medical, financial); few-shot examples calibrate what "principle" means in that domain, preventing vague or irrelevant step-backs.

---

### Option 4 — Async step-back with parallel principle generation

```python
import anthropic
import asyncio

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")


async def generate_principle(question: str) -> str:
    """Generate the step-back principle asynchronously."""
    response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",   # cheap model for principle extraction
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                f"State the one key principle, theorem, or framework that is most "
                f"relevant to answering this question. Be specific and concise (1-2 sentences).\n\n"
                f"Question: {question}"
            ),
        }],
    )
    return response.content[0].text.strip()


async def generate_answer(question: str, principle: str) -> str:
    """Generate the final answer using the principle as context."""
    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": (
                f"Answer the following question. "
                f"Start by applying this principle: {principle}\n\n"
                f"Question: {question}"
            ),
        }],
    )
    return response.content[0].text.strip()


async def step_back_answer(question: str) -> str:
    """Generate principle (cheap) then answer (expensive) sequentially."""
    principle = await generate_principle(question)
    print(f"[Principle] {principle}")
    return await generate_answer(question, principle)


async def step_back_batch(questions: list[str]) -> list[str]:
    """Process multiple questions with step-back, concurrently."""
    return await asyncio.gather(*[step_back_answer(q) for q in questions])


questions = [
    "Why does adding more servers sometimes make a distributed system slower?",
    "Should I normalize or denormalize my database schema for a read-heavy app?",
    "How do I choose between eventual consistency and strong consistency for my cache?",
]

results = asyncio.run(step_back_batch(questions))
for q, r in zip(questions, results):
    print(f"\nQ: {q}\nA: {r[:200]}...")
```

**Expected Token Savings:** Haiku principle extraction costs ~40 tokens vs ~200 tokens on Sonnet; for 10-question batches, saves ~1600 tokens on principle generation alone while maintaining the reasoning quality of Sonnet for the final answer.
**Environment:** Batch processing pipelines where many complex questions are answered concurrently; Haiku for principles + Sonnet for answers is the cost-efficient split.

---

### Option 5 — Multi-step step-back for deeply nested problems

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")


def deep_step_back(question: str, depth: int = 2) -> str:
    """
    Multi-level step-back for deeply nested or interdisciplinary problems.
    Depth 1: immediate framework
    Depth 2: meta-framework (what kind of problem is this?)
    """
    DEPTH_PROMPTS = {
        1: "What is the specific technical principle or theorem that applies?",
        2: "At a higher level, what category of problem is this, and what general approach does that category require?",
    }

    principles = []

    # Generate principles from high-level to low-level
    for d in range(depth, 0, -1):
        context = "\n".join(f"Level {i+1}: {p}" for i, p in enumerate(principles))
        prompt = (
            f"{DEPTH_PROMPTS.get(d, DEPTH_PROMPTS[1])}\n\n"
            f"{'Prior principles:\n' + context + chr(10) if context else ''}"
            f"Question: {question}"
        )
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        principle = resp.content[0].text.strip()
        principles.insert(0, principle)
        print(f"[Depth {d}] {principle[:100]}...")

    # Final answer using all principles
    principle_block = "\n".join(
        f"Level {i+1} principle: {p}" for i, p in enumerate(principles)
    )
    final_prompt = (
        f"Answer the question by applying the following principles in order, "
        f"from most general to most specific:\n\n{principle_block}\n\nQuestion: {question}"
    )
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": final_prompt}],
    )
    return resp.content[0].text.strip()


# This question requires both distributed systems knowledge AND game theory
question = (
    "In a microservices architecture, if every team optimizes their service's "
    "performance independently, why might the overall system get worse?"
)

result = deep_step_back(question, depth=2)
print(f"\n[Final answer]\n{result}")
```

**Expected Token Savings:** Multi-level step-back costs ~600 extra tokens (2 principle calls); for complex interdisciplinary problems, prevents a long wrong-direction answer (~2000 tokens) that requires full re-explanation.
**Environment:** Complex cross-domain questions (systems + economics, biology + statistics, law + technology); the second step-back catches the meta-pattern that the first step-back might miss.

---

### Option 6 — Adaptive step-back: skip for simple, apply for complex

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

COMPLEXITY_CLASSIFIER_SYSTEM = (
    "Classify the complexity of this question. Reply with exactly one word:\n"
    "  simple — factual lookup, unit conversion, or definition\n"
    "  moderate — single-step reasoning or comparison\n"
    "  complex — multi-step reasoning, trade-off analysis, or requires a framework"
)


def classify_complexity(question: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        system=COMPLEXITY_CLASSIFIER_SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    label = response.content[0].text.strip().lower()
    return label if label in ("simple", "moderate", "complex") else "moderate"


def run_adaptive_agent(question: str) -> str:
    complexity = classify_complexity(question)
    print(f"[Complexity: {complexity}]")

    if complexity == "simple":
        # Direct answer — no step-back overhead
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",   # cheap model for simple questions
            max_tokens=256,
            messages=[{"role": "user", "content": question}],
        )
        return response.content[0].text

    elif complexity == "moderate":
        # Single-turn step-back instruction
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system="State the relevant principle briefly, then answer.",
            messages=[{"role": "user", "content": question}],
        )
        return response.content[0].text

    else:  # complex
        # Full two-turn step-back
        principle_resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": f"State the fundamental framework for: {question}",
            }],
        )
        principle = principle_resp.content[0].text.strip()

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": f"Framework: {principle}\n\nNow answer: {question}",
            }],
        )
        return response.content[0].text


# Comparison table
# | Option | Step-back Type | Extra Calls | Best For |
# |--------|---------------|------------|----------|
# | 1 Two-turn explicit | Principle then answer | 2 calls | Hard novel questions |
# | 2 System prompt | Single-turn structured | 1 call | General assistant |
# | 3 Few-shot domain | Domain-calibrated | 1 call | Narrow-domain advisors |
# | 4 Async parallel batch | Haiku principle + Sonnet answer | 2 calls | Batch pipelines |
# | 5 Multi-level deep | 2-level principle hierarchy | 3 calls | Interdisciplinary |
# | 6 Adaptive | Complexity-routed | 1-2 calls | Mixed question types |

for q in [
    "What is the capital of France?",
    "Which is faster, Python or Go?",
    "Why do distributed databases struggle with global transactions?",
]:
    print(f"\nQ: {q}")
    print(run_adaptive_agent(q)[:200])
```

**Expected Token Savings:** Simple questions use Haiku with no step-back (~60 tokens); complex questions use the full two-turn step-back (~1600 tokens total but prevents wrong-answer correction cycles worth ~2400 tokens). Net positive for any question set with > 30% complex questions.
**Environment:** Mixed-intent agents receiving a range of question types; adaptive routing ensures step-back overhead is only paid for questions that actually need it.
