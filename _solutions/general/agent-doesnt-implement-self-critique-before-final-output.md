---
layout: solution
title: "Agent Doesn't Implement Self-Critique Before Final Output"
category: general
description: "Add a self-critique pass where the agent reviews its own draft response for errors, gaps, and quality before returning the final answer to the user."
tags: [self-critique, reflection, quality, review, chain-of-thought, output-quality]
---

# Agent Doesn't Implement Self-Critique Before Final Output

Agents return their first draft as the final answer, even when that draft contains logical errors, missing steps, or vague claims. A self-critique pass asks the model to review its own output against explicit quality criteria, identify weaknesses, and produce a revised version — yielding measurably better outputs for complex tasks at the cost of one extra API call.

## Option 1: Simple Draft-then-Critique Loop

```python
import anthropic

client = anthropic.Anthropic()

CRITIQUE_PROMPT = """Review your previous response for:
1. Factual accuracy — are all claims correct?
2. Completeness — are any important points missing?
3. Clarity — is anything ambiguous or confusing?
4. Conciseness — is there unnecessary verbosity?

If the response is already excellent, reply: "APPROVED: <response>"
Otherwise, reply with an improved version that fixes the issues."""


def agent_with_self_critique(user_prompt: str) -> str:
    # Step 1: Generate draft
    draft_r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": user_prompt}],
    )
    draft = draft_r.content[0].text
    print(f"[DRAFT] {draft[:100]}...")

    # Step 2: Self-critique
    critique_r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": draft},
            {"role": "user", "content": CRITIQUE_PROMPT},
        ],
    )
    revised = critique_r.content[0].text

    if revised.startswith("APPROVED:"):
        print("[CRITIQUE] Approved without changes")
        return draft
    print("[CRITIQUE] Revised output produced")
    return revised


if __name__ == "__main__":
    result = agent_with_self_critique(
        "Explain the difference between concurrency and parallelism in Python."
    )
    print("\n=== Final Output ===\n", result)

# Expected Token Savings: One extra call (~500 tokens) prevents multi-turn correction cycles
# Environment: Python 3.9+; use haiku for both draft and critique to minimize cost
```

## Option 2: Rubric-Based Critique with Structured Scores

```python
import json
import anthropic

client = anthropic.Anthropic()

RUBRIC_PROMPT = """Evaluate the following response using this rubric (score each 1-5):
- Accuracy: Are all technical claims correct?
- Completeness: Does it cover all aspects of the question?
- Clarity: Is it easy to understand for the target audience?
- Actionability: Can the reader act on this information?

Question: {question}

Response to evaluate:
{response}

Return JSON: {{"scores": {{"accuracy": N, "completeness": N, "clarity": N, "actionability": N}}, "issues": ["..."], "verdict": "approve|revise"}}"""

REVISION_PROMPT = """Improve the response based on these issues:
{issues}

Original question: {question}

Write the improved response now:"""


def critique_and_revise(question: str, response: str) -> tuple[str, dict]:
    # Score with rubric
    rubric_r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": RUBRIC_PROMPT.format(
            question=question, response=response[:800]
        )}],
    )
    try:
        evaluation = json.loads(rubric_r.content[0].text)
    except json.JSONDecodeError:
        return response, {"verdict": "approve", "scores": {}}

    scores = evaluation.get("scores", {})
    avg_score = sum(scores.values()) / len(scores) if scores else 5
    issues = evaluation.get("issues", [])
    verdict = evaluation.get("verdict", "approve")

    print(f"[RUBRIC] avg={avg_score:.1f} verdict={verdict} issues={len(issues)}")

    if verdict == "revise" or avg_score < 3.5:
        issues_text = "\n".join(f"- {i}" for i in issues)
        revision_r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": REVISION_PROMPT.format(
                issues=issues_text, question=question
            )}],
        )
        return revision_r.content[0].text, evaluation

    return response, evaluation


def agent(question: str) -> str:
    draft_r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": question}],
    )
    draft = draft_r.content[0].text
    final, eval_data = critique_and_revise(question, draft)
    return final


if __name__ == "__main__":
    result = agent("How does Python's GIL affect multi-threaded performance?")
    print(result)

# Expected Token Savings: Rubric critique adds ~200 tokens; prevents low-quality outputs reaching users
# Environment: Python 3.9+; tune avg_score threshold (3.5) based on your quality floor
```

## Option 3: Async Parallel Draft + Critique with Best-of Selection

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

SELF_SCORE_PROMPT = """Rate the following response on overall quality (1-10).
Consider: accuracy, completeness, clarity, and usefulness.
Return only a JSON object: {{"score": <int>, "reason": "<one sentence>"}}

Question: {question}
Response: {response}"""


async def generate_draft(question: str, style: str) -> str:
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": f"{question}\n\n(Style hint: {style})"}],
    )
    return r.content[0].text


async def score_draft(question: str, draft: str) -> float:
    import json
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": SELF_SCORE_PROMPT.format(
            question=question, response=draft[:600]
        )}],
    )
    try:
        data = json.loads(r.content[0].text)
        return float(data.get("score", 5))
    except (json.JSONDecodeError, ValueError):
        return 5.0


async def best_of_n(question: str, n: int = 3) -> str:
    styles = [
        "detailed with examples",
        "concise and direct",
        "structured with bullet points",
    ][:n]

    # Generate all drafts in parallel
    drafts = await asyncio.gather(*[generate_draft(question, s) for s in styles])

    # Score all drafts in parallel
    scores = await asyncio.gather(*[score_draft(question, d) for d in drafts])

    for i, (draft, score) in enumerate(zip(drafts, scores)):
        print(f"[DRAFT {i+1}] score={score:.1f} style='{styles[i]}'")

    best_idx = scores.index(max(scores))
    return drafts[best_idx]


async def main() -> None:
    result = await best_of_n("What is the best way to handle exceptions in async Python code?", n=3)
    print("\n=== Best Draft ===\n", result)


asyncio.run(main())

# Expected Token Savings: Best-of-3 costs 6x single call but avoids user follow-up corrections
# Environment: Python 3.11+; reduce n to 2 for cost savings in production
```

## Option 4: Critique with Fact-Check Pass Using Tool Calls

```python
import anthropic
import json

client = anthropic.Anthropic()

FACT_CHECK_TOOLS = [
    {
        "name": "flag_uncertain_claim",
        "description": "Flag a claim in the response that may be inaccurate or unverifiable",
        "input_schema": {
            "type": "object",
            "properties": {
                "claim": {"type": "string", "description": "The exact claim to flag"},
                "reason": {"type": "string", "description": "Why this claim might be wrong or uncertain"},
                "severity": {"type": "string", "enum": ["minor", "major", "critical"]},
            },
            "required": ["claim", "reason", "severity"],
        },
    }
]

FACT_CHECK_SYSTEM = """You are a critical fact-checker. Review the given response and call flag_uncertain_claim for each claim that is:
- Potentially incorrect
- Overly broad or unverifiable
- Missing important nuance
Only flag real issues, not nitpicks. If the response is fully accurate, call no tools."""


def fact_check_response(question: str, draft: str) -> list[dict]:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=FACT_CHECK_SYSTEM,
        tools=FACT_CHECK_TOOLS,
        messages=[{
            "role": "user",
            "content": f"Question: {question}\n\nResponse to fact-check:\n{draft}",
        }],
    )
    flags = []
    for block in r.content:
        if block.type == "tool_use" and block.name == "flag_uncertain_claim":
            flags.append(block.input)
    return flags


REVISION_PROMPT = """The following response has been fact-checked and issues were found.

Original question: {question}

Original response:
{draft}

Issues found:
{issues}

Write a corrected response that fixes all flagged issues:"""


def agent_with_fact_check(question: str) -> str:
    # Generate draft
    draft_r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": question}],
    )
    draft = draft_r.content[0].text

    # Fact-check
    flags = fact_check_response(question, draft)
    if not flags:
        print("[FACT-CHECK] No issues found")
        return draft

    print(f"[FACT-CHECK] {len(flags)} issue(s) found:")
    for f in flags:
        print(f"  [{f['severity'].upper()}] {f['claim'][:60]}: {f['reason'][:60]}")

    # Revise
    issues_text = "\n".join(
        f"- [{f['severity'].upper()}] '{f['claim']}': {f['reason']}"
        for f in flags
    )
    revision_r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": REVISION_PROMPT.format(
            question=question, draft=draft, issues=issues_text
        )}],
    )
    return revision_r.content[0].text


if __name__ == "__main__":
    result = agent_with_fact_check(
        "What are the key differences between asyncio and threading in Python?"
    )
    print("\n=== Verified Output ===\n", result)

# Expected Token Savings: Tool-based flagging is precise; revision only triggered when needed
# Environment: Python 3.9+; use claude-sonnet-4-6 for fact-checking sensitive/factual content
```

## Option 5: Iterative Critique Loop with Early Exit

```python
import anthropic

client = anthropic.Anthropic()

MAX_ITERATIONS = 3
QUALITY_THRESHOLD = 8  # out of 10

ITERATIVE_CRITIQUE = """Review this response and decide if it needs improvement.

Question: {question}
Response: {response}

Reply in this exact format:
SCORE: <integer 1-10>
VERDICT: <approve|revise>
ISSUES: <comma-separated list of issues, or 'none'>
REVISION: <improved response if verdict is revise, else leave blank>"""


def extract_fields(text: str) -> dict:
    result = {"score": 5, "verdict": "approve", "issues": [], "revision": ""}
    for line in text.strip().split("\n"):
        if line.startswith("SCORE:"):
            try:
                result["score"] = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("VERDICT:"):
            result["verdict"] = line.split(":", 1)[1].strip().lower()
        elif line.startswith("ISSUES:"):
            raw = line.split(":", 1)[1].strip()
            result["issues"] = [] if raw == "none" else [i.strip() for i in raw.split(",")]
        elif line.startswith("REVISION:"):
            result["revision"] = line.split(":", 1)[1].strip()
    return result


def iterative_self_critique(question: str) -> str:
    # Generate initial draft
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": question}],
    )
    current = r.content[0].text

    for iteration in range(MAX_ITERATIONS):
        critique_r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=768,
            messages=[{"role": "user", "content": ITERATIVE_CRITIQUE.format(
                question=question, response=current
            )}],
        )
        fields = extract_fields(critique_r.content[0].text)
        score = fields["score"]
        verdict = fields["verdict"]

        print(f"[ITER {iteration+1}] score={score} verdict={verdict} issues={fields['issues'][:2]}")

        if verdict == "approve" or score >= QUALITY_THRESHOLD:
            print(f"[CRITIQUE] Accepted at iteration {iteration+1}")
            break

        if fields["revision"]:
            current = fields["revision"]
        else:
            break

    return current


if __name__ == "__main__":
    result = iterative_self_critique(
        "Explain how Python's memory management and garbage collection work."
    )
    print("\n=== Final Output ===\n", result)

# Expected Token Savings: Early exit on high scores avoids wasted iterations; max 3 rounds
# Environment: Python 3.9+; increase QUALITY_THRESHOLD to 9 for higher-stakes outputs
```

## Option 6: Async Multi-Agent Critique Pipeline (Draft + Devil's Advocate + Synthesizer)

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

DEVILS_ADVOCATE_PROMPT = """You are a devil's advocate. Your job is to find flaws, counterarguments, and missing perspectives in the following response.

Question: {question}
Response: {response}

List 2-3 specific weaknesses or counterpoints. Be constructive but critical."""

SYNTHESIZER_PROMPT = """You have a draft response and a critic's feedback. Write a final response that:
1. Preserves the strengths of the draft
2. Addresses the critic's valid concerns
3. Is concise and well-organized

Question: {question}
Draft: {draft}
Critic's feedback: {critique}

Write the final synthesized response:"""


async def generate_draft(question: str) -> str:
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": question}],
    )
    return r.content[0].text


async def devils_advocate(question: str, draft: str) -> str:
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": DEVILS_ADVOCATE_PROMPT.format(
            question=question, response=draft[:600]
        )}],
    )
    return r.content[0].text


async def synthesize(question: str, draft: str, critique: str) -> str:
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": SYNTHESIZER_PROMPT.format(
            question=question, draft=draft[:400], critique=critique[:400]
        )}],
    )
    return r.content[0].text


async def multi_agent_critique(question: str) -> str:
    # Phase 1: Generate draft
    draft = await generate_draft(question)
    print(f"[DRAFT] {draft[:80]}...")

    # Phase 2: Devil's advocate critique (can run in parallel with any other phase 2 work)
    critique = await devils_advocate(question, draft)
    print(f"[CRITIC] {critique[:80]}...")

    # Phase 3: Synthesize best of both
    final = await synthesize(question, draft, critique)
    return final


async def main() -> None:
    result = await multi_agent_critique(
        "What are the trade-offs between microservices and monolithic architecture for AI agent systems?"
    )
    print("\n=== Synthesized Final Output ===\n", result)


asyncio.run(main())

# Expected Token Savings: 3-agent pipeline (~1200 tokens) vs. multi-turn user correction (3000+ tokens)
# Environment: Python 3.11+; replace haiku with sonnet for the synthesizer on high-value outputs
```

## Comparison

| Option | Critique Method | Iterations | Parallel | Cost Overhead | Best For |
|--------|----------------|-----------|----------|--------------|----------|
| 1. Simple Loop | Self-review prompt | 1 | No | Low (~500t) | General responses |
| 2. Rubric Scores | Structured rubric JSON | 1 | No | Medium (~300t) | Quality-gated outputs |
| 3. Best-of-N | Self-scoring + select | 1 (parallel drafts) | Yes | High (3x drafts) | High-stakes answers |
| 4. Fact-Check Tools | Tool-call flagging | 1 | No | Medium | Factual/technical content |
| 5. Iterative Loop | Score + early exit | Up to 3 | No | Variable | Progressive refinement |
| 6. Multi-Agent | Draft + critic + synth | 1 each | Partial | Medium (~1200t) | Complex trade-off questions |
