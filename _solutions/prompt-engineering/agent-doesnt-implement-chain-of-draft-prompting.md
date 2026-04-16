---
title: "Agent Doesn't Implement Chain-of-Draft Prompting"
description: "AI agents jump directly to final answers without structured intermediate drafting — producing lower-quality outputs on complex tasks that benefit from iterative refinement: legal analysis, code review, strategic planning, and multi-step reasoning."
problem_description: |
  When an agent is asked to produce a high-quality output — a detailed code review, a strategic memo, a complex explanation — it generates a single response in one shot. The model has no opportunity to catch logical gaps, revise unclear sections, or improve completeness through iterative self-review. Chain-of-draft prompting asks the model to produce an initial draft, critique it, then produce an improved final version — all within a single prompt or a short multi-turn sequence. This two-pass approach reliably improves output quality on tasks where completeness, accuracy, and clarity matter.
category: prompt-engineering
difficulty: intermediate
tags: [chain-of-draft, prompting, output-quality, iterative-refinement, reasoning]
---

## Solution 1: Single-Turn Chain-of-Draft Prompt

Instruct the model to produce a draft, self-critique, then deliver the final response — all in one turn — using explicit XML tags to structure each phase.

```python
import asyncio
from anthropic import AsyncAnthropic


CHAIN_OF_DRAFT_SYSTEM = """You are a careful, high-quality writer.
For every response, follow this structure:

<draft>
Your initial answer — complete but unpolished.
</draft>

<critique>
Identify gaps, inaccuracies, unclear sections, and missing examples.
Be specific. List at least 3 issues if any exist.
</critique>

<final>
Your improved, polished answer addressing all critique points.
This is what the user receives.
</final>"""


def extract_final(response_text: str) -> str:
    """Extract the <final> section from a chain-of-draft response."""
    import re
    match = re.search(r'<final>(.*?)</final>', response_text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return response_text  # Fallback: return full text


def extract_all_phases(response_text: str) -> dict[str, str]:
    import re
    phases = {}
    for tag in ("draft", "critique", "final"):
        match = re.search(rf'<{tag}>(.*?)</{tag}>', response_text, re.DOTALL)
        if match:
            phases[tag] = match.group(1).strip()
    return phases


async def chain_of_draft(
    client: AsyncAnthropic,
    user_message: str,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 2048,
    show_phases: bool = False,
) -> str:
    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=CHAIN_OF_DRAFT_SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    full_text = response.content[0].text

    if show_phases:
        phases = extract_all_phases(full_text)
        for phase, content in phases.items():
            print(f"\n=== {phase.upper()} ===\n{content[:200]}...")

    return extract_final(full_text)


# Usage
async def main():
    client = AsyncAnthropic()

    tasks = [
        "Explain the CAP theorem and its practical implications for distributed system design.",
        "Write a code review checklist for Python async/await code.",
    ]

    for task in tasks:
        print(f"\nTask: {task}")
        answer = await chain_of_draft(client, task, show_phases=True)
        print(f"\nFinal answer ({len(answer)} chars): {answer[:300]}...")

asyncio.run(main())
```

## Solution 2: Multi-Turn Draft-Revise Agent

Use a two-turn conversation — first turn generates the draft, second turn asks for targeted revision based on explicit quality criteria — giving the model more context for each phase.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass


@dataclass
class DraftReviseResult:
    draft: str
    critique: str
    final: str
    draft_tokens: int
    revision_tokens: int


REVISION_PROMPT_TEMPLATE = """Here is your draft response:

<draft>
{draft}
</draft>

Please review it against these quality criteria:
{criteria}

Identify specific issues, then produce an improved final version.

Format:
<issues>
[List specific problems found]
</issues>

<revised>
[Improved final response]
</revised>"""


async def draft_then_revise(
    client: AsyncAnthropic,
    user_message: str,
    quality_criteria: list[str],
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 1024,
) -> DraftReviseResult:
    import re

    # Turn 1: Generate initial draft
    draft_response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system="You are a careful, knowledgeable assistant.",
        messages=[{"role": "user", "content": user_message}],
    )
    draft = draft_response.content[0].text
    draft_tokens = draft_response.usage.output_tokens

    # Turn 2: Revise against criteria
    criteria_str = "\n".join(f"- {c}" for c in quality_criteria)
    revision_prompt = REVISION_PROMPT_TEMPLATE.format(
        draft=draft,
        criteria=criteria_str,
    )

    revision_response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system="You are a careful editor who improves technical writing.",
        messages=[
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": draft},
            {"role": "user", "content": revision_prompt},
        ],
    )
    revision_text = revision_response.content[0].text
    revision_tokens = revision_response.usage.output_tokens

    # Extract sections
    issues_match = re.search(r'<issues>(.*?)</issues>', revision_text, re.DOTALL)
    revised_match = re.search(r'<revised>(.*?)</revised>', revision_text, re.DOTALL)

    critique = issues_match.group(1).strip() if issues_match else ""
    final = revised_match.group(1).strip() if revised_match else revision_text

    return DraftReviseResult(
        draft=draft,
        critique=critique,
        final=final,
        draft_tokens=draft_tokens,
        revision_tokens=revision_tokens,
    )


# Usage
async def main():
    client = AsyncAnthropic()

    result = await draft_then_revise(
        client,
        user_message="Explain how database connection pooling works and when to use it.",
        quality_criteria=[
            "Includes a concrete analogy to aid understanding",
            "Covers both benefits and tradeoffs",
            "Mentions at least two specific pool parameters (e.g., min_size, max_size)",
            "Provides a practical 'when to use' guideline",
            "Under 400 words",
        ],
    )

    print(f"Draft tokens: {result.draft_tokens}")
    print(f"Revision tokens: {result.revision_tokens}")
    print(f"\nCritique:\n{result.critique}")
    print(f"\nFinal Answer:\n{result.final}")

asyncio.run(main())
```

## Solution 3: Adaptive Chain-of-Draft — Skip Drafting for Simple Queries

Classify query complexity before deciding whether to apply chain-of-draft — simple factual queries get direct answers; complex analytical queries get the full draft-critique-final pipeline.

```python
import asyncio
import re
from anthropic import AsyncAnthropic
from dataclasses import dataclass
from enum import Enum


class Complexity(str, Enum):
    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"


@dataclass
class AdaptiveResult:
    answer: str
    complexity: Complexity
    used_drafting: bool
    total_tokens: int


COMPLEXITY_CLASSIFIER_SYSTEM = """Classify the complexity of the following question.
Reply with exactly one word: simple, moderate, or complex.

simple: factual lookup, definition, yes/no
moderate: explanation requiring 2-3 steps, comparison of 2 things
complex: multi-step analysis, tradeoffs across many dimensions, design decisions, code review"""

DRAFT_SYSTEM = """Produce a high-quality answer in two phases:

<thinking>
Plan your answer: key points to cover, potential pitfalls, best structure.
</thinking>

<answer>
Your final, polished response.
</answer>"""


async def classify_complexity(client: AsyncAnthropic, query: str) -> Complexity:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",  # Cheap classifier
        max_tokens=10,
        system=COMPLEXITY_CLASSIFIER_SYSTEM,
        messages=[{"role": "user", "content": query}],
    )
    label = response.content[0].text.strip().lower()
    try:
        return Complexity(label)
    except ValueError:
        return Complexity.MODERATE


async def answer_direct(client: AsyncAnthropic, query: str) -> tuple[str, int]:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": query}],
    )
    return response.content[0].text, response.usage.output_tokens


async def answer_with_draft(client: AsyncAnthropic, query: str, model: str) -> tuple[str, int]:
    response = await client.messages.create(
        model=model,
        max_tokens=1024,
        system=DRAFT_SYSTEM,
        messages=[{"role": "user", "content": query}],
    )
    text = response.content[0].text
    match = re.search(r'<answer>(.*?)</answer>', text, re.DOTALL)
    answer = match.group(1).strip() if match else text
    return answer, response.usage.output_tokens


async def adaptive_chain_of_draft(
    client: AsyncAnthropic,
    query: str,
    complex_model: str = "claude-sonnet-4-6",
) -> AdaptiveResult:
    complexity = await classify_complexity(client, query)
    print(f"Complexity: {complexity.value}")

    if complexity == Complexity.SIMPLE:
        answer, tokens = await answer_direct(client, query)
        return AdaptiveResult(answer, complexity, used_drafting=False, total_tokens=tokens)
    elif complexity == Complexity.MODERATE:
        answer, tokens = await answer_with_draft(client, query, "claude-haiku-4-5-20251001")
        return AdaptiveResult(answer, complexity, used_drafting=True, total_tokens=tokens)
    else:  # COMPLEX
        answer, tokens = await answer_with_draft(client, query, complex_model)
        return AdaptiveResult(answer, complexity, used_drafting=True, total_tokens=tokens)


# Usage
async def main():
    client = AsyncAnthropic()

    queries = [
        "What year was Python created?",
        "What is the difference between a list and a tuple in Python?",
        "Design a fault-tolerant distributed task queue that handles agent failures and guarantees exactly-once processing.",
    ]

    for query in queries:
        result = await adaptive_chain_of_draft(client, query)
        print(f"\nQ: {query}")
        print(f"Drafted: {result.used_drafting} | Tokens: {result.total_tokens}")
        print(f"A: {result.answer[:200]}")

asyncio.run(main())
```

## Solution 4: Parallel Multi-Draft with Best-of-N Selection

Generate N independent drafts in parallel, then use a judge call to select the best one — trading cost for quality on high-stakes outputs.

```python
import asyncio
import re
from anthropic import AsyncAnthropic
from dataclasses import dataclass


@dataclass
class DraftCandidate:
    index: int
    text: str
    token_count: int


@dataclass
class BestOfNResult:
    winner_index: int
    winner_text: str
    judge_reasoning: str
    all_candidates: list[DraftCandidate]
    total_tokens: int


JUDGE_SYSTEM = """You are a quality judge. You will receive N draft answers to the same question.
Select the best answer and explain why.

Evaluate on: accuracy, completeness, clarity, appropriate depth, practical utility.

Reply in this format:
<winner>N</winner>
<reasoning>Why this draft is best, and what the others lacked.</reasoning>"""


async def generate_draft(
    client: AsyncAnthropic,
    query: str,
    index: int,
    model: str,
    max_tokens: int,
    temperature_note: str = "",
) -> DraftCandidate:
    system = "You are a knowledgeable, precise assistant." + (
        f"\n\n{temperature_note}" if temperature_note else ""
    )
    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": query}],
    )
    return DraftCandidate(
        index=index,
        text=response.content[0].text,
        token_count=response.usage.output_tokens,
    )


async def judge_drafts(
    client: AsyncAnthropic,
    query: str,
    candidates: list[DraftCandidate],
    model: str = "claude-sonnet-4-6",
) -> tuple[int, str, int]:
    drafts_text = "\n\n".join([
        f"<draft_{c.index}>\n{c.text}\n</draft_{c.index}>"
        for c in candidates
    ])
    judge_prompt = f"Question: {query}\n\n{drafts_text}"

    response = await client.messages.create(
        model=model,
        max_tokens=512,
        system=JUDGE_SYSTEM,
        messages=[{"role": "user", "content": judge_prompt}],
    )
    text = response.content[0].text

    winner_match = re.search(r'<winner>(\d+)</winner>', text)
    reasoning_match = re.search(r'<reasoning>(.*?)</reasoning>', text, re.DOTALL)

    winner_idx = int(winner_match.group(1)) if winner_match else 0
    reasoning = reasoning_match.group(1).strip() if reasoning_match else text

    return winner_idx, reasoning, response.usage.output_tokens


async def best_of_n(
    client: AsyncAnthropic,
    query: str,
    n: int = 3,
    draft_model: str = "claude-haiku-4-5-20251001",
    judge_model: str = "claude-sonnet-4-6",
    max_tokens: int = 512,
) -> BestOfNResult:
    # Generate N drafts in parallel
    draft_tasks = [
        generate_draft(client, query, i, draft_model, max_tokens)
        for i in range(n)
    ]
    candidates = await asyncio.gather(*draft_tasks)

    # Judge selects best
    winner_idx, reasoning, judge_tokens = await judge_drafts(
        client, query, list(candidates), judge_model
    )

    winner = next((c for c in candidates if c.index == winner_idx), candidates[0])
    total_tokens = sum(c.token_count for c in candidates) + judge_tokens

    return BestOfNResult(
        winner_index=winner_idx,
        winner_text=winner.text,
        judge_reasoning=reasoning,
        all_candidates=list(candidates),
        total_tokens=total_tokens,
    )


# Usage
async def main():
    client = AsyncAnthropic()

    result = await best_of_n(
        client,
        query="What are the tradeoffs between microservices and monolithic architectures?",
        n=3,
    )

    print(f"Winner: Draft #{result.winner_index}")
    print(f"Judge reasoning: {result.judge_reasoning[:200]}")
    print(f"Total tokens: {result.total_tokens}")
    print(f"\nWinning answer:\n{result.winner_text[:400]}")

asyncio.run(main())
```

## Solution 5: Structured Draft with Rubric-Driven Self-Scoring

Ask the model to score its own draft against a rubric before revising — making the self-critique quantitative and auditable rather than qualitative and vague.

```python
import asyncio
import json
import re
from anthropic import AsyncAnthropic
from dataclasses import dataclass


@dataclass
class RubricScore:
    criterion: str
    score: int  # 1–5
    feedback: str


@dataclass
class RubricScoredResult:
    draft: str
    scores: list[RubricScore]
    total_score: int
    max_score: int
    revised: str


RUBRIC_DRAFT_SYSTEM = """You are a rigorous self-improving assistant.

Phase 1 — Draft:
Write your initial answer in <draft></draft> tags.

Phase 2 — Score:
Score your draft against each criterion (1–5). Use this JSON format inside <scores></scores>:
[
  {{"criterion": "criterion name", "score": N, "feedback": "specific issue or praise"}},
  ...
]

Phase 3 — Revise:
Write an improved answer in <revised></revised> that addresses all low-scoring criteria."""


def parse_scores(text: str) -> list[RubricScore]:
    match = re.search(r'<scores>(.*?)</scores>', text, re.DOTALL)
    if not match:
        return []
    try:
        raw = json.loads(match.group(1).strip())
        return [RubricScore(**item) for item in raw]
    except (json.JSONDecodeError, TypeError):
        return []


def extract_tag(text: str, tag: str) -> str:
    match = re.search(rf'<{tag}>(.*?)</{tag}>', text, re.DOTALL)
    return match.group(1).strip() if match else ""


async def rubric_draft_revise(
    client: AsyncAnthropic,
    query: str,
    rubric: list[str],
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 1500,
) -> RubricScoredResult:
    rubric_section = "\n".join(f"- {criterion}" for criterion in rubric)
    full_prompt = f"""Answer this question: {query}

Evaluation rubric (score each 1–5):
{rubric_section}"""

    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=RUBRIC_DRAFT_SYSTEM,
        messages=[{"role": "user", "content": full_prompt}],
    )
    text = response.content[0].text

    draft = extract_tag(text, "draft")
    revised = extract_tag(text, "revised")
    scores = parse_scores(text)

    total = sum(s.score for s in scores)
    max_score = len(scores) * 5

    return RubricScoredResult(
        draft=draft,
        scores=scores,
        total_score=total,
        max_score=max_score,
        revised=revised,
    )


# Usage
async def main():
    client = AsyncAnthropic()

    result = await rubric_draft_revise(
        client,
        query="Explain how to design an idempotent API endpoint.",
        rubric=[
            "Defines idempotency clearly with an example",
            "Explains at least 2 implementation techniques",
            "Covers HTTP methods and their idempotency properties",
            "Mentions error handling and retry scenarios",
            "Concise: under 350 words",
        ],
    )

    print("Scores:")
    for s in result.scores:
        print(f"  [{s.score}/5] {s.criterion}: {s.feedback}")
    print(f"\nTotal: {result.total_score}/{result.max_score}")
    print(f"\nRevised answer:\n{result.revised[:400]}")

asyncio.run(main())
```

## Solution 6: Domain-Specific Chain-of-Draft Templates

Maintain task-type-specific draft templates (code review, technical writing, strategic planning) that structure each phase with domain-relevant prompts rather than generic draft/critique/final.

```python
import asyncio
import re
from anthropic import AsyncAnthropic
from dataclasses import dataclass
from typing import Callable


@dataclass
class DraftTemplate:
    name: str
    system_prompt: str
    response_extractor: Callable[[str], str]


def extract_xml(tag: str) -> Callable[[str], str]:
    def extractor(text: str) -> str:
        match = re.search(rf'<{tag}>(.*?)</{tag}>', text, re.DOTALL)
        return match.group(1).strip() if match else text
    return extractor


CODE_REVIEW_TEMPLATE = DraftTemplate(
    name="code_review",
    system_prompt="""Review code in these phases:

<issues>
List bugs, security problems, performance issues, and style violations.
Format each as: [SEVERITY: critical/major/minor] Description
</issues>

<positives>
List 2-3 things done well.
</positives>

<recommendations>
Prioritized, actionable improvements with code examples where helpful.
</recommendations>""",
    response_extractor=lambda text: "\n\n".join([
        s for tag in ["issues", "positives", "recommendations"]
        if (m := re.search(rf'<{tag}>(.*?)</{tag}>', text, re.DOTALL))
        for s in [f"**{tag.upper()}**\n" + m.group(1).strip()]
    ]),
)

TECHNICAL_EXPLANATION_TEMPLATE = DraftTemplate(
    name="technical_explanation",
    system_prompt="""Explain in this structure:

<core_concept>
1-sentence definition a non-expert could understand.
</core_concept>

<how_it_works>
Step-by-step mechanism with a concrete analogy.
</how_it_works>

<when_to_use>
Specific scenarios with tradeoffs mentioned.
</when_to_use>

<gotchas>
2-3 common mistakes or misunderstandings.
</gotchas>""",
    response_extractor=lambda text: "\n\n".join([
        s for tag in ["core_concept", "how_it_works", "when_to_use", "gotchas"]
        if (m := re.search(rf'<{tag}>(.*?)</{tag}>', text, re.DOTALL))
        for s in [f"**{tag.replace('_', ' ').title()}**\n" + m.group(1).strip()]
    ]),
)

STRATEGIC_PLAN_TEMPLATE = DraftTemplate(
    name="strategic_plan",
    system_prompt="""Plan in this structure:

<situation>
Current state, problem being solved.
</situation>

<options>
3 distinct approaches with pros/cons each.
</options>

<recommendation>
Preferred option with rationale and key assumptions.
</recommendation>

<risks>
Top 3 risks and mitigations.
</risks>""",
    response_extractor=lambda text: "\n\n".join([
        s for tag in ["situation", "options", "recommendation", "risks"]
        if (m := re.search(rf'<{tag}>(.*?)</{tag}>', text, re.DOTALL))
        for s in [f"**{tag.upper()}**\n" + m.group(1).strip()]
    ]),
)

TEMPLATES = {
    "code_review": CODE_REVIEW_TEMPLATE,
    "technical_explanation": TECHNICAL_EXPLANATION_TEMPLATE,
    "strategic_plan": STRATEGIC_PLAN_TEMPLATE,
}


async def templated_draft(
    client: AsyncAnthropic,
    template_name: str,
    content: str,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 1500,
) -> str:
    template = TEMPLATES[template_name]
    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=template.system_prompt,
        messages=[{"role": "user", "content": content}],
    )
    return template.response_extractor(response.content[0].text)


# Usage
async def main():
    client = AsyncAnthropic()

    code = """
async def fetch_user(user_id):
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute(f"SELECT * FROM users WHERE id = {user_id}")
    return cursor.fetchone()
"""

    print("=== CODE REVIEW ===")
    review = await templated_draft(client, "code_review", code)
    print(review[:600])

    print("\n=== TECHNICAL EXPLANATION ===")
    explanation = await templated_draft(
        client, "technical_explanation",
        "Explain what a database index is and when to use one."
    )
    print(explanation[:600])

asyncio.run(main())
```

## Comparison

| Approach | Quality Gain | Token Overhead | Latency | Auditability | Best For |
|---|---|---|---|---|---|
| Single-Turn CoD | Medium | ~1.5x | Minimal | Low | General improvement, low complexity |
| Multi-Turn Draft-Revise | High | ~2x | Medium | Medium | Targeted revision against criteria |
| Adaptive (complexity-routed) | Variable | Minimal–2x | Low–Medium | Medium | Mixed-complexity workloads |
| Parallel Best-of-N | Very High | N×draft + judge | Medium | High | High-stakes, cost-tolerant outputs |
| Rubric Self-Scoring | High | ~2x | Medium | Very High | Measurable quality standards |
| Domain Templates | High | ~1.5x | Minimal | High | Structured domain-specific outputs |
