---
layout: solution
title: "Agent Doesn't Implement Conversation Branching for Parallel Exploration"
category: general
description: "Explore multiple solution paths simultaneously by forking conversation history into parallel branches and merging the best results."
tags: [branching, parallel, exploration, multi-path, conversation, strategy]
---

# Agent Doesn't Implement Conversation Branching for Parallel Exploration

When an agent faces an ambiguous problem, it commits to a single solution path immediately. This means suboptimal first choices compound into wrong final answers. Conversation branching lets the agent fork its history, explore N approaches in parallel, evaluate each branch's outcome, and merge the best result — like `git branch` for reasoning.

## Option 1: Simple Two-Branch Fork with Best-Pick

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()


async def explore_branch(messages: list[dict], branch_prompt: str) -> tuple[str, str]:
    branch_messages = messages + [{"role": "user", "content": branch_prompt}]
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=branch_messages,
    )
    return branch_prompt, response.content[0].text


async def branch_and_pick(base_messages: list[dict], alternatives: list[str]) -> str:
    """Explore all alternatives in parallel, return the longest/richest response."""
    tasks = [explore_branch(base_messages, alt) for alt in alternatives]
    results = await asyncio.gather(*tasks)

    # Pick the branch with the most substantive response
    best_prompt, best_response = max(results, key=lambda r: len(r[1]))
    print(f"[BRANCH] Selected: {best_prompt[:60]}...")
    return best_response


async def main() -> None:
    base = [{"role": "user", "content": "I need to process 10,000 customer records daily."}]
    alternatives = [
        "Approach A: Use a batch pipeline with chunked API calls.",
        "Approach B: Use a streaming queue with per-record processing.",
        "Approach C: Use a map-reduce pattern with parallel workers.",
    ]
    answer = await branch_and_pick(base, alternatives)
    print("Best branch result:\n", answer)


asyncio.run(main())

# Expected Token Savings: Parallel branches cost 3x tokens but avoid multi-turn dead ends
# Environment: Python 3.11+, asyncio; scale alternatives list to your token budget
```

## Option 2: Scored Branch Evaluation with Haiku Judge

```python
import asyncio
import anthropic
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()


@dataclass
class Branch:
    label: str
    prompt: str
    response: str
    score: float = 0.0


JUDGE_PROMPT = """Rate the following agent response on a scale of 1-10 for:
- Correctness (does it solve the problem?)
- Completeness (does it cover edge cases?)
- Clarity (is it easy to understand?)

Response to rate:
{response}

Return only a JSON object: {{"score": <float 1-10>, "reason": "<one sentence>"}}"""


async def run_branch(base_messages: list[dict], label: str, approach: str) -> Branch:
    msgs = base_messages + [{"role": "user", "content": approach}]
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=msgs,
    )
    return Branch(label=label, prompt=approach, response=r.content[0].text)


async def judge_branch(branch: Branch) -> Branch:
    import json
    judge_msgs = [{"role": "user", "content": JUDGE_PROMPT.format(response=branch.response[:800])}]
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=judge_msgs,
    )
    try:
        data = json.loads(r.content[0].text)
        branch.score = float(data.get("score", 5.0))
    except (json.JSONDecodeError, ValueError):
        branch.score = 5.0
    return branch


async def parallel_branch_explore(
    base_messages: list[dict], approaches: dict[str, str]
) -> Branch:
    # Phase 1: Generate all branches in parallel
    gen_tasks = [run_branch(base_messages, label, prompt)
                 for label, prompt in approaches.items()]
    branches = await asyncio.gather(*gen_tasks)

    # Phase 2: Judge all branches in parallel
    judge_tasks = [judge_branch(b) for b in branches]
    scored = await asyncio.gather(*judge_tasks)

    best = max(scored, key=lambda b: b.score)
    for b in sorted(scored, key=lambda b: b.score, reverse=True):
        print(f"  [{b.score:.1f}] {b.label}")
    return best


async def main() -> None:
    base = [{"role": "user", "content": "Design a caching layer for an AI agent."}]
    approaches = {
        "in-memory-lru": "Use an in-memory LRU cache with TTL expiry.",
        "redis-cache": "Use Redis with key-based invalidation.",
        "sqlite-cache": "Use SQLite with a background vacuum task.",
    }
    best = await parallel_branch_explore(base, approaches)
    print(f"\nWinner: {best.label} (score={best.score:.1f})\n{best.response}")


asyncio.run(main())

# Expected Token Savings: Judge adds ~80 haiku tokens per branch; prevents multi-turn rework
# Environment: Python 3.11+; replace judge with domain-specific rubric for your use case
```

## Option 3: Tree Search with Depth-Limited Branch Pruning

```python
import asyncio
import anthropic
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()
MAX_DEPTH = 2
BRANCH_FACTOR = 2


@dataclass
class Node:
    depth: int
    messages: list[dict]
    response: str = ""
    children: list["Node"] = field(default_factory=list)
    score: float = 0.0


async def expand_node(node: Node, question: str) -> None:
    """Ask the model to suggest BRANCH_FACTOR continuations, then pick the best."""
    suggest_prompt = (
        f"Given this conversation so far, suggest {BRANCH_FACTOR} distinct next steps "
        f"to answer: '{question}'. Number them 1. and 2."
    )
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=node.messages + [{"role": "user", "content": suggest_prompt}],
    )
    suggestions_text = r.content[0].text
    lines = [l.strip() for l in suggestions_text.split("\n") if l.strip()]
    suggestions = [l for l in lines if l[:2] in ("1.", "2.")][:BRANCH_FACTOR]

    async def make_child(suggestion: str) -> Node:
        child_msgs = node.messages + [
            {"role": "assistant", "content": suggestions_text},
            {"role": "user", "content": suggestion},
        ]
        cr = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=child_msgs,
        )
        child = Node(depth=node.depth + 1, messages=child_msgs, response=cr.content[0].text)
        child.score = len(cr.content[0].text)  # simple heuristic
        return child

    tasks = [make_child(s) for s in suggestions]
    node.children = await asyncio.gather(*tasks)


async def tree_search(initial_question: str, depth: int = MAX_DEPTH) -> str:
    root = Node(
        depth=0,
        messages=[{"role": "user", "content": initial_question}],
    )
    r0 = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=root.messages,
    )
    root.response = r0.content[0].text
    root.messages.append({"role": "assistant", "content": root.response})

    queue = [root]
    all_leaves: list[Node] = []

    for _ in range(depth):
        next_queue: list[Node] = []
        await asyncio.gather(*[expand_node(n, initial_question) for n in queue])
        for n in queue:
            if n.children:
                next_queue.extend(n.children)
            else:
                all_leaves.append(n)
        queue = next_queue
    all_leaves.extend(queue)

    best = max(all_leaves, key=lambda n: n.score)
    print(f"[TREE] Explored {len(all_leaves)} leaf nodes; best score={best.score}")
    return best.response


async def main() -> None:
    result = await tree_search("How should I handle rate limits in an agent that calls external APIs?")
    print("Best path answer:\n", result)


asyncio.run(main())

# Expected Token Savings: Tree search prevents committing to bad paths early
# Environment: Python 3.11+; tune MAX_DEPTH and BRANCH_FACTOR for cost vs. quality
```

## Option 4: Branching with SQLite History and Best-Branch Persistence

```python
import asyncio
import sqlite3
import time
import json
import anthropic

DB_PATH = "branches.db"
client = anthropic.AsyncAnthropic()


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS branches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            branch_label TEXT,
            approach TEXT,
            response TEXT,
            score REAL,
            created_at REAL,
            selected INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    return conn


async def generate_branch(
    session_id: str, label: str, base_context: str, approach: str, conn: sqlite3.Connection
) -> dict:
    msgs = [
        {"role": "user", "content": base_context},
        {"role": "user", "content": approach},
    ]
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=msgs,
    )
    response = r.content[0].text
    # Heuristic score: length + keyword richness
    score = len(response) / 100 + response.count("```") * 2.0

    conn.execute(
        "INSERT INTO branches VALUES (NULL,?,?,?,?,?,?,0)",
        (session_id, label, approach, response, score, time.time()),
    )
    conn.commit()
    return {"label": label, "response": response, "score": score}


async def branch_explore_with_db(
    session_id: str, base_context: str, approaches: dict[str, str]
) -> str:
    conn = init_db()
    tasks = [
        generate_branch(session_id, label, base_context, approach, conn)
        for label, approach in approaches.items()
    ]
    results = await asyncio.gather(*tasks)
    best = max(results, key=lambda r: r["score"])

    conn.execute(
        "UPDATE branches SET selected=1 WHERE session_id=? AND branch_label=?",
        (session_id, best["label"]),
    )
    conn.commit()

    # Print branch report
    rows = conn.execute(
        "SELECT branch_label, score, selected FROM branches WHERE session_id=? ORDER BY score DESC",
        (session_id,),
    ).fetchall()
    print("\n=== Branch Report ===")
    for label, score, selected in rows:
        marker = " <-- SELECTED" if selected else ""
        print(f"  {label}: score={score:.2f}{marker}")

    conn.close()
    return best["response"]


async def main() -> None:
    session_id = f"sess_{int(time.time())}"
    base = "We need to design a fault-tolerant task queue for an AI agent system."
    approaches = {
        "sqlite-queue": "Use SQLite with lease-based claiming and dead-letter queue.",
        "redis-streams": "Use Redis Streams with consumer groups and acknowledgement.",
        "postgres-queue": "Use PostgreSQL SKIP LOCKED for atomic task claiming.",
    }
    answer = await branch_explore_with_db(session_id, base, approaches)
    print("\nSelected answer:\n", answer[:400])


asyncio.run(main())

# Expected Token Savings: SQLite history enables post-session analysis of branch quality
# Environment: Python 3.11+, SQLite3; query branches table for quality trend analysis
```

## Option 5: Branching with Synthesis — Merge Best Elements

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

SYNTHESIS_PROMPT = """You are given {n} different draft responses to the same question.
Synthesize the best elements from each into a single, superior response.

Question: {question}

{drafts}

Write the synthesized response now:"""


async def draft_branch(question: str, angle: str) -> str:
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": f"{question}\n\nApproach: {angle}"}],
    )
    return r.content[0].text


async def synthesize(question: str, drafts: list[str]) -> str:
    draft_block = "\n\n".join(
        f"Draft {i+1}:\n{d}" for i, d in enumerate(drafts)
    )
    prompt = SYNTHESIS_PROMPT.format(n=len(drafts), question=question, drafts=draft_block)
    r = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    return r.content[0].text


async def branch_and_synthesize(question: str, angles: list[str]) -> str:
    print(f"[BRANCH] Generating {len(angles)} branches in parallel...")
    drafts = await asyncio.gather(*[draft_branch(question, a) for a in angles])

    print("[BRANCH] Synthesizing best elements with Sonnet...")
    final = await synthesize(question, list(drafts))
    return final


async def main() -> None:
    question = "What is the best strategy for handling context window limits in a long-running agent?"
    angles = [
        "Focus on summarization and compression techniques.",
        "Focus on hierarchical memory and retrieval.",
        "Focus on task decomposition and checkpointing.",
    ]
    result = await branch_and_synthesize(question, angles)
    print("Synthesized answer:\n", result)


asyncio.run(main())

# Expected Token Savings: Synthesis produces one high-quality answer instead of multi-turn iteration
# Environment: Python 3.11+; use haiku for drafts, sonnet/opus for synthesis step
```

## Option 6: Confidence-Gated Branching — Branch Only When Uncertain

```python
import asyncio
import json
import anthropic

client = anthropic.AsyncAnthropic()

CONFIDENCE_PROMPT = """Answer the question below. Also rate your confidence from 0.0 to 1.0.
Return JSON: {{"answer": "<your answer>", "confidence": <float>, "uncertainty_reason": "<why uncertain if < 0.8>"}}

Question: {question}"""

BRANCH_ANGLES = [
    "Consider the performance and scalability angle.",
    "Consider the reliability and fault-tolerance angle.",
    "Consider the cost and resource efficiency angle.",
]


async def get_with_confidence(question: str) -> dict:
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": CONFIDENCE_PROMPT.format(question=question)}],
    )
    try:
        return json.loads(r.content[0].text)
    except json.JSONDecodeError:
        return {"answer": r.content[0].text, "confidence": 0.5, "uncertainty_reason": "parse error"}


async def branch_on_uncertainty(question: str, confidence_threshold: float = 0.8) -> str:
    initial = await get_with_confidence(question)
    confidence = initial.get("confidence", 0.0)
    print(f"[BRANCH] Initial confidence: {confidence:.2f}")

    if confidence >= confidence_threshold:
        print("[BRANCH] High confidence — returning direct answer.")
        return initial["answer"]

    print(f"[BRANCH] Low confidence ({initial.get('uncertainty_reason', '?')}) — branching...")
    branches = await asyncio.gather(*[
        get_with_confidence(f"{question}\n\nHint: {angle}")
        for angle in BRANCH_ANGLES
    ])

    # Pick branch with highest confidence
    all_candidates = [initial] + list(branches)
    best = max(all_candidates, key=lambda r: r.get("confidence", 0.0))
    best_conf = best.get("confidence", 0.0)
    print(f"[BRANCH] Best branch confidence: {best_conf:.2f}")

    if best_conf >= confidence_threshold:
        return best["answer"]

    # Still uncertain — synthesize
    combined = "\n\n".join(
        f"Perspective (conf={c.get('confidence',0):.2f}): {c['answer']}"
        for c in all_candidates
    )
    r = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{"role": "user", "content": f"Synthesize these perspectives into one answer for: {question}\n\n{combined}"}],
    )
    return r.content[0].text


async def main() -> None:
    answer = await branch_on_uncertainty(
        "Should I use async generators or async queues for streaming tool results?"
    )
    print("Final answer:\n", answer)


asyncio.run(main())

# Expected Token Savings: Branching triggered only on low confidence — saves tokens on easy questions
# Environment: Python 3.11+; tune confidence_threshold based on acceptable quality floor
```

## Comparison

| Option | Branching Strategy | Judge | Merging | Token Cost | Best For |
|--------|-------------------|-------|---------|------------|----------|
| 1. Simple Fork | Fixed alternatives | None | Best-length | Low | Fast exploration |
| 2. Scored Branches | Fixed alternatives | Haiku judge | Best-score | Medium | Quality-scored selection |
| 3. Tree Search | Model-generated | Heuristic | Best leaf | High | Deep problem spaces |
| 4. SQLite History | Fixed alternatives | Heuristic | Best-score + DB | Low | Audit + trending |
| 5. Synthesis | Fixed alternatives | None | Sonnet merge | Medium | Combined best-of-all |
| 6. Confidence-Gated | Dynamic on uncertainty | Self-reported | Best-confidence | Variable | Cost-efficient exploration |
