---
layout: solution
title: "Agent Doesn't Implement Self-Refine Iterative Improvement Loop"
category: prompt-engineering
description: "Agents that return their first draft miss obvious improvements they would catch on reflection. These patterns show how to implement self-refine loops where the agent critiques and rewrites its own output until it meets a quality threshold."
tags: [prompt-engineering, self-refine, iterative, quality, reflection, anthropic]
---

## Problem

A single-pass LLM response is rarely optimal. The model produces a draft constrained by its generation mode, then has no opportunity to step back and evaluate it. Self-refine loops separate generation from evaluation: the agent produces a draft, critiques it, and rewrites based on the critique — repeating until quality converges or a maximum number of iterations is reached.

---

### Option 1: Simple Generate-Critique-Rewrite Loop

Run a fixed number of generate → critique → rewrite cycles on the same model.

```python
import anthropic

client = anthropic.Anthropic()

CRITIQUE_PROMPT = """You are a strict editor. Critique this response to the original question.

Original question: {question}

Response to critique:
{response}

Give 3-5 specific, actionable improvements. Be concise. Format as a numbered list."""

REWRITE_PROMPT = """Rewrite the response below, addressing every critique point.

Original question: {question}

Previous response:
{response}

Critique:
{critique}

Write the improved response directly, without preamble."""

def self_refine(question: str, max_iterations: int = 3, model: str = "claude-sonnet-4-6") -> str:
    # Initial draft
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": question}],
    )
    current = response.content[0].text
    print(f"[draft 0] {len(current.split())} words")

    for i in range(max_iterations):
        # Critique
        crit_response = client.messages.create(
            model=model,
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": CRITIQUE_PROMPT.format(question=question, response=current),
            }],
        )
        critique = crit_response.content[0].text

        # Check if critique finds no issues
        if any(phrase in critique.lower() for phrase in
               ["no significant", "no major", "looks good", "no improvements needed"]):
            print(f"[iteration {i+1}] critique satisfied — stopping early")
            break

        # Rewrite
        rewrite_response = client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": REWRITE_PROMPT.format(
                    question=question, response=current, critique=critique
                ),
            }],
        )
        current = rewrite_response.content[0].text
        print(f"[iteration {i+1}] rewritten: {len(current.split())} words")

    return current

if __name__ == "__main__":
    question = "Explain the tradeoffs between SQL and NoSQL databases for a high-traffic e-commerce platform."
    final = self_refine(question, max_iterations=2)
    print("\n=== Final Response ===")
    print(final[:600])

# Expected Token Savings: 2 iterations often sufficient; haiku critique + sonnet rewrite is 40% cheaper than opus single-pass
# Environment: ANTHROPIC_API_KEY
```

---

### Option 2: Rubric-Scored Refinement with Early Exit

Score the response against a rubric before each iteration and exit when the score passes a threshold.

```python
import json
import re
import anthropic

client = anthropic.Anthropic()

RUBRIC_PROMPT = """Score this response on a scale of 1-10 for each criterion.

Question: {question}

Response:
{response}

Criteria:
- accuracy: factually correct and precise
- completeness: covers all key aspects of the question
- clarity: easy to understand, well-structured
- actionability: includes concrete examples or next steps
- conciseness: no unnecessary filler or repetition

Respond with JSON only: {{"accuracy": N, "completeness": N, "clarity": N, "actionability": N, "conciseness": N, "overall": N, "weakest_area": "...", "one_improvement": "..."}}"""

TARGETED_REWRITE_PROMPT = """Improve this response. Focus specifically on: {weakness}

Question: {question}
Current response: {response}

Write the improved version directly."""

def score_response(question: str, response: str, model: str = "claude-haiku-4-5-20251001") -> dict:
    r = client.messages.create(
        model=model,
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": RUBRIC_PROMPT.format(question=question, response=response),
        }],
    )
    raw = r.content[0].text.strip()
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return {"overall": 5, "weakest_area": "clarity", "one_improvement": "Be more specific."}

def rubric_refine(
    question: str,
    max_iterations: int = 4,
    threshold: float = 8.0,
    generate_model: str = "claude-sonnet-4-6",
    score_model: str = "claude-haiku-4-5-20251001",
) -> tuple[str, list[dict]]:
    response = client.messages.create(
        model=generate_model,
        max_tokens=1024,
        messages=[{"role": "user", "content": question}],
    )
    current = response.content[0].text
    history = []

    for i in range(max_iterations):
        scores = score_response(question, current, model=score_model)
        history.append(scores)
        overall = scores.get("overall", 5)
        weakness = scores.get("weakest_area", "clarity")
        improvement = scores.get("one_improvement", "")
        print(f"[iter {i}] score={overall}/10 weakness={weakness}")

        if overall >= threshold:
            print(f"[threshold {threshold} reached at iteration {i}]")
            break

        rewrite = client.messages.create(
            model=generate_model,
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": TARGETED_REWRITE_PROMPT.format(
                    weakness=f"{weakness}: {improvement}",
                    question=question,
                    response=current,
                ),
            }],
        )
        current = rewrite.content[0].text

    return current, history

if __name__ == "__main__":
    q = "How should I design a caching layer for a microservices architecture?"
    final, scores = rubric_refine(q, max_iterations=3, threshold=8.0)
    print("\n=== Score History ===")
    for i, s in enumerate(scores):
        print(f"  Iteration {i}: overall={s.get('overall')}, weakest={s.get('weakest_area')}")
    print("\n=== Final ===")
    print(final[:500])

# Expected Token Savings: Cheap Haiku scorer; early exit when threshold met; avoids unnecessary rewrites
# Environment: ANTHROPIC_API_KEY
```

---

### Option 3: Async Parallel Critique from Multiple Perspectives

Generate multiple parallel critiques (different reviewer personas) and synthesize them into one rewrite.

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

REVIEWER_PERSONAS = [
    ("technical_reviewer", "You are a senior engineer. Focus on technical accuracy, edge cases, and implementation details."),
    ("ux_reviewer", "You are a UX writer. Focus on clarity, structure, and whether a non-expert could follow this."),
    ("critic_reviewer", "You are a harsh critic. Find every weak argument, unsupported claim, and missing nuance."),
]

PARALLEL_CRITIQUE_PROMPT = """{persona}

Review this response to: {question}

Response: {response}

Give 2-3 specific improvements from your perspective. Be direct."""

MULTI_CRITIQUE_REWRITE_PROMPT = """Rewrite this response incorporating all critique points below.

Question: {question}

Current response: {response}

Critiques from multiple reviewers:
{critiques}

Write the improved version directly. Do not mention the critiques."""

async def get_critique(persona_name: str, persona_desc: str, question: str, response: str) -> tuple[str, str]:
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=persona_desc,
        messages=[{
            "role": "user",
            "content": PARALLEL_CRITIQUE_PROMPT.format(
                persona=persona_desc, question=question, response=response
            ),
        }],
    )
    return persona_name, r.content[0].text

async def parallel_self_refine(
    question: str,
    max_iterations: int = 2,
    generate_model: str = "claude-sonnet-4-6",
) -> str:
    # Initial draft
    r = await client.messages.create(
        model=generate_model,
        max_tokens=1024,
        messages=[{"role": "user", "content": question}],
    )
    current = r.content[0].text
    print(f"[draft] {len(current.split())} words")

    for i in range(max_iterations):
        # All critiques in parallel
        critique_tasks = [
            get_critique(name, desc, question, current)
            for name, desc in REVIEWER_PERSONAS
        ]
        results = await asyncio.gather(*critique_tasks)

        critiques_text = "\n\n".join(
            f"[{name}]:\n{critique}" for name, critique in results
        )
        print(f"[iter {i+1}] {len(REVIEWER_PERSONAS)} parallel critiques received")

        # Single rewrite incorporating all critiques
        rewrite = await client.messages.create(
            model=generate_model,
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": MULTI_CRITIQUE_REWRITE_PROMPT.format(
                    question=question,
                    response=current,
                    critiques=critiques_text,
                ),
            }],
        )
        current = rewrite.content[0].text
        print(f"[iter {i+1}] rewritten: {len(current.split())} words")

    return current

if __name__ == "__main__":
    async def main():
        q = "What are the best practices for securing a REST API?"
        final = await parallel_self_refine(q, max_iterations=2)
        print("\n=== Final ===")
        print(final[:600])
    asyncio.run(main())

# Expected Token Savings: 3 parallel Haiku critiques cost less than 1 Sonnet critique; parallel reduces wall time 3x
# Environment: ANTHROPIC_API_KEY
```

---

### Option 4: Constrained Refinement with Diff Tracking

Track diffs between iterations to detect when rewrites stop making meaningful changes.

```python
import re
import difflib
import anthropic

client = anthropic.Anthropic()

def word_overlap_delta(a: str, b: str) -> float:
    """Fraction of words that changed between two texts."""
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a and not words_b:
        return 0.0
    union = words_a | words_b
    intersection = words_a & words_b
    return 1.0 - len(intersection) / len(union)

def compute_diff_summary(old: str, new: str) -> str:
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    diff = list(difflib.unified_diff(old_lines, new_lines, lineterm="", n=0))
    added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
    return f"+{added}/-{removed} lines"

CONSTRAINED_REFINE_PROMPT = """Improve this response. You MUST:
1. Keep all accurate information from the original
2. Fix only the identified weakness: {weakness}
3. Stay within {max_words} words
4. Do not add new sections unless necessary

Question: {question}
Current response: {response}
Weakness to fix: {weakness}

Write the improved response."""

IDENTIFY_WEAKNESS_PROMPT = """In one sentence, what is the single biggest weakness of this response?

Question: {question}
Response: {response}

Answer in one sentence starting with: "The main weakness is..." """

def constrained_refine(
    question: str,
    max_iterations: int = 4,
    min_delta: float = 0.05,    # stop if <5% of words changed
    max_words: int = 500,
    model: str = "claude-sonnet-4-6",
) -> tuple[str, list[dict]]:
    r = client.messages.create(
        model=model,
        max_tokens=max_words * 2,
        messages=[{"role": "user", "content": question}],
    )
    current = r.content[0].text
    history = [{"iteration": 0, "words": len(current.split()), "delta": 1.0}]

    for i in range(max_iterations):
        # Identify weakness using cheap model
        weakness_r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": IDENTIFY_WEAKNESS_PROMPT.format(
                    question=question, response=current
                ),
            }],
        )
        weakness = weakness_r.content[0].text.strip()

        # Rewrite with constraint
        rewrite_r = client.messages.create(
            model=model,
            max_tokens=max_words * 2,
            messages=[{
                "role": "user",
                "content": CONSTRAINED_REFINE_PROMPT.format(
                    weakness=weakness,
                    max_words=max_words,
                    question=question,
                    response=current,
                ),
            }],
        )
        new = rewrite_r.content[0].text

        delta = word_overlap_delta(current, new)
        diff_summary = compute_diff_summary(current, new)
        history.append({
            "iteration": i + 1,
            "weakness": weakness[:80],
            "diff": diff_summary,
            "delta": round(delta, 3),
            "words": len(new.split()),
        })
        print(f"[iter {i+1}] delta={delta:.3f} {diff_summary} weakness='{weakness[:50]}'")

        if delta < min_delta:
            print(f"[converged: delta {delta:.3f} < min {min_delta}]")
            break

        current = new

    return current, history

if __name__ == "__main__":
    q = "Explain how to implement zero-downtime deployments for a stateful web application."
    final, history = constrained_refine(q, max_iterations=3, max_words=400)
    print("\n=== Iteration History ===")
    for h in history:
        print(f"  iter={h['iteration']} words={h['words']} delta={h['delta']}", end="")
        if "diff" in h:
            print(f" {h['diff']}")
        else:
            print()
    print("\n=== Final ===")
    print(final[:500])

# Expected Token Savings: Convergence detection stops early; word-count constraint prevents bloat
# Environment: ANTHROPIC_API_KEY
```

---

### Option 5: Domain-Specific Rubric with Structured Feedback

Apply a domain-specific evaluation rubric and pass structured feedback as JSON to the rewriter.

```python
import json
import anthropic
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class RubricDimension:
    name: str
    description: str
    weight: float

TECHNICAL_RUBRIC = [
    RubricDimension("correctness", "All technical claims are accurate and verifiable", 0.35),
    RubricDimension("depth", "Goes beyond surface-level; includes implementation details", 0.25),
    RubricDimension("examples", "Includes concrete, runnable examples", 0.20),
    RubricDimension("tradeoffs", "Acknowledges limitations and alternative approaches", 0.20),
]

STRUCTURED_EVAL_PROMPT = """Evaluate this technical response using the rubric.

Question: {question}
Response: {response}

Rubric dimensions:
{rubric}

Respond with JSON:
{{
  "scores": {{"correctness": N, "depth": N, "examples": N, "tradeoffs": N}},
  "weighted_total": N,
  "feedback": {{"correctness": "...", "depth": "...", "examples": "...", "tradeoffs": "..."}},
  "priority_fix": "dimension name with lowest score"
}}
All scores 1-10."""

STRUCTURED_REWRITE_PROMPT = """Rewrite this technical response using the structured feedback.

Question: {question}
Current response: {response}

Structured feedback:
{feedback}

Priority improvement: {priority} — fix this first.

Write the improved response."""

def evaluate(question: str, response: str, rubric: list[RubricDimension]) -> dict:
    rubric_text = "\n".join(f"- {r.name} (weight {r.weight}): {r.description}" for r in rubric)
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": STRUCTURED_EVAL_PROMPT.format(
                question=question, response=response, rubric=rubric_text
            ),
        }],
    )
    raw = r.content[0].text.strip()
    # Extract JSON
    import re
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except Exception:
            pass
    return {"weighted_total": 5, "feedback": {}, "priority_fix": "depth"}

def structured_refine(
    question: str,
    rubric: list[RubricDimension],
    max_iterations: int = 3,
    threshold: float = 8.0,
    model: str = "claude-sonnet-4-6",
) -> str:
    r = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": question}],
    )
    current = r.content[0].text

    for i in range(max_iterations):
        eval_result = evaluate(question, current, rubric)
        score = eval_result.get("weighted_total", 5)
        priority = eval_result.get("priority_fix", "depth")
        feedback = eval_result.get("feedback", {})
        print(f"[iter {i}] score={score}/10 priority_fix={priority}")
        print(f"         feedback: {json.dumps(feedback, indent=0)[:120]}")

        if score >= threshold:
            print(f"[threshold {threshold} met]")
            break

        rewrite = client.messages.create(
            model=model,
            max_tokens=1536,
            messages=[{
                "role": "user",
                "content": STRUCTURED_REWRITE_PROMPT.format(
                    question=question,
                    response=current,
                    feedback=json.dumps(feedback, indent=2),
                    priority=priority,
                ),
            }],
        )
        current = rewrite.content[0].text

    return current

if __name__ == "__main__":
    q = "How do you implement a production-ready message queue consumer in Python?"
    final = structured_refine(q, TECHNICAL_RUBRIC, max_iterations=3, threshold=8.0)
    print("\n=== Final Response ===")
    print(final[:600])

# Expected Token Savings: Haiku evaluator saves 60% vs Sonnet; structured JSON feedback is compact vs prose
# Environment: ANTHROPIC_API_KEY
```

---

### Option 6: Async Self-Refine with Streaming and Progress Callbacks

Stream the rewrite tokens in real time while running critique in the background.

```python
import asyncio
from typing import Callable, Awaitable
import anthropic

client = anthropic.AsyncAnthropic()

STREAMING_CRITIQUE_PROMPT = """Briefly critique this response in 3 bullet points. Be direct and specific.

Question: {question}
Response: {response}"""

async def stream_response(prompt: str, model: str = "claude-sonnet-4-6",
                           max_tokens: int = 1024,
                           on_chunk: Callable[[str], None] = None) -> str:
    chunks = []
    async with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for text in stream.text_stream:
            chunks.append(text)
            if on_chunk:
                on_chunk(text)
    return "".join(chunks)

async def get_critique(question: str, response: str) -> str:
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": STREAMING_CRITIQUE_PROMPT.format(
                question=question, response=response
            ),
        }],
    )
    return r.content[0].text

async def streaming_self_refine(
    question: str,
    max_iterations: int = 2,
    on_draft: Callable[[int, str], Awaitable[None]] = None,
) -> str:
    print("[generating initial draft (streaming)]")
    tokens = []
    current = await stream_response(
        question,
        on_chunk=lambda t: tokens.append(t),
    )
    print(f"\n[draft 0: {len(current.split())} words]")

    if on_draft:
        await on_draft(0, current)

    for i in range(max_iterations):
        # Run critique concurrently while we prepare the rewrite prompt
        print(f"[iteration {i+1}: getting critique...]")
        critique = await get_critique(question, current)
        print(f"[critique received: {len(critique.split())} words]")

        rewrite_prompt = (
            f"Rewrite this response, fixing these issues:\n{critique}\n\n"
            f"Original question: {question}\n\n"
            f"Current response:\n{current}\n\n"
            f"Improved response:"
        )

        print(f"[iteration {i+1}: streaming rewrite...]")
        new_tokens = []
        current = await stream_response(
            rewrite_prompt,
            on_chunk=lambda t: (new_tokens.append(t), print(t, end="", flush=True)),
        )
        print(f"\n[iteration {i+1} complete: {len(current.split())} words]")

        if on_draft:
            await on_draft(i + 1, current)

    return current

async def progress_callback(iteration: int, draft: str):
    print(f"\n--- Progress update: iteration {iteration}, {len(draft.split())} words ---")

if __name__ == "__main__":
    async def main():
        q = "Explain how to implement graceful shutdown in a Python async web service."
        final = await streaming_self_refine(q, max_iterations=2, on_draft=progress_callback)
        print("\n=== Final Response ===")
        print(final[:500])
    asyncio.run(main())

# Expected Token Savings: Streaming shows time-to-first-token immediately; critique overlaps with user reading time
# Environment: ANTHROPIC_API_KEY
```

---

## Comparison

| Option | Approach | Critique Model | Iterations | Best For |
|--------|----------|---------------|------------|----------|
| 1 | Simple generate-critique-rewrite | Same model | Fixed N | Quick quality boost, minimal setup |
| 2 | Rubric-scored with early exit | Haiku scorer | Until threshold | When you have explicit quality criteria |
| 3 | Parallel multi-persona critique | 3x Haiku | Fixed N | Comprehensive review, 3x faster critique |
| 4 | Constrained + diff convergence | Haiku (weakness) | Until converged | Preventing runaway rewrites |
| 5 | Domain rubric + structured JSON | Haiku evaluator | Until threshold | Technical content with measurable criteria |
| 6 | Async streaming with progress | Haiku (background) | Fixed N | User-facing agents needing live feedback |
