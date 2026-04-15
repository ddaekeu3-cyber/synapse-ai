---
layout: solution
title: "Agent Doesn't Implement Tree-of-Thought Reasoning"
category: prompt-engineering
description: "How to implement Tree-of-Thought (ToT) prompting where the agent explores multiple reasoning branches, evaluates each path, and selects the most promising route before producing a final answer."
tags: [prompt-engineering, reasoning, tree-of-thought, planning, search, quality]
---

# Agent Doesn't Implement Tree-of-Thought Reasoning

Standard chain-of-thought generates one linear reasoning path. For complex problems with multiple valid approaches, this misses better solutions that require exploring and backtracking. Tree-of-Thought (ToT) generates multiple candidate reasoning paths, evaluates their promise, and selects the best branch — mimicking human deliberate problem-solving.

## Option 1: Simple Branch-and-Select ToT

Generate N candidate reasoning paths in parallel, score each, and continue with the highest-scoring branch.

```python
import anthropic
import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class ThoughtBranch:
    branch_id: int
    reasoning: str
    score: float
    conclusion: Optional[str] = None


def generate_branches(client: anthropic.Anthropic, problem: str, n_branches: int = 3) -> list[ThoughtBranch]:
    """Generate N independent reasoning paths for the problem."""
    branches = []

    for i in range(n_branches):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=250,
            system=f"You are reasoning approach #{i+1}. Take a distinct perspective from other approaches.",
            messages=[{"role": "user", "content": (
                f"Problem: {problem}\n\n"
                f"Provide a step-by-step reasoning path (approach #{i+1}). "
                "Think differently from standard approaches. Be explicit about each step."
            )}],
        )
        branches.append(ThoughtBranch(
            branch_id=i + 1,
            reasoning=response.content[0].text,
        ))

    return branches


def score_branches(client: anthropic.Anthropic, problem: str, branches: list[ThoughtBranch]) -> list[ThoughtBranch]:
    """Score each branch for correctness, completeness, and insight."""
    scored = []
    for branch in branches:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content": (
                f"Problem: {problem}\n\n"
                f"Evaluate this reasoning path:\n{branch.reasoning[:400]}\n\n"
                "Rate from 0.0 to 1.0 on: correctness, completeness, and insight. "
                "Reply with JSON: {\"score\": 0.0-1.0, \"verdict\": \"one sentence\"}"
            )}],
        )
        text = response.content[0].text
        try:
            json_match = re.search(r"\{[^}]+\}", text, re.DOTALL)
            data = __import__("json").loads(json_match.group()) if json_match else {}
            score = float(data.get("score", 0.5))
        except Exception:
            score = 0.5
        branch.score = score
        scored.append(branch)

    scored.sort(key=lambda b: -b.score)
    return scored


def synthesize_from_best_branch(
    client: anthropic.Anthropic,
    problem: str,
    best_branch: ThoughtBranch,
) -> str:
    """Produce the final answer by following the best reasoning path."""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        messages=[{"role": "user", "content": (
            f"Problem: {problem}\n\n"
            f"Based on this reasoning path (score={best_branch.score:.2f}):\n"
            f"{best_branch.reasoning[:500]}\n\n"
            "Provide the final, definitive answer."
        )}],
    )
    return response.content[0].text


def tree_of_thought(problem: str, n_branches: int = 3) -> str:
    client = anthropic.Anthropic()

    print(f"[ToT] Generating {n_branches} reasoning branches...")
    branches = generate_branches(client, problem, n_branches)

    print("[ToT] Scoring branches...")
    scored_branches = score_branches(client, problem, branches)

    for b in scored_branches:
        print(f"  Branch {b.branch_id}: score={b.score:.2f} | {b.reasoning[:60]}...")

    best = scored_branches[0]
    print(f"[ToT] Selected branch {best.branch_id} (score={best.score:.2f})")

    return synthesize_from_best_branch(client, problem, best)


if __name__ == "__main__":
    problem = (
        "A company has 3 servers. Server A handles 40% of traffic, "
        "Server B handles 35%, and Server C handles 25%. "
        "If Server B fails, how should traffic be redistributed to minimize latency "
        "assuming A and C have the same latency characteristics?"
    )
    print(f"Problem: {problem}\n")
    answer = tree_of_thought(problem, n_branches=3)
    print(f"\nFinal Answer:\n{answer}")

# Expected Token Savings: Produces higher-quality answers in fewer total turns vs. iterative back-and-forth; reduces correction loops
# Environment: Complex reasoning tasks, math problems, strategic decisions where the first approach is often suboptimal
```

## Option 2: Depth-First Tree Search with Pruning

Build a reasoning tree depth-first: expand the most promising node, score it, prune dead ends, and backtrack when stuck.

```python
import anthropic
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TreeNode:
    node_id: str
    depth: int
    thought: str
    score: float = 0.0
    children: list = field(default_factory=list)
    parent_id: Optional[str] = None
    is_terminal: bool = False
    is_pruned: bool = False


class ThoughtTree:
    def __init__(self, problem: str):
        self.problem = problem
        self.nodes: dict[str, TreeNode] = {}
        self._counter = 0

    def new_node(self, thought: str, depth: int, parent_id: Optional[str] = None) -> TreeNode:
        self._counter += 1
        node = TreeNode(
            node_id=f"N{self._counter}",
            depth=depth,
            thought=thought,
            parent_id=parent_id,
        )
        self.nodes[node.node_id] = node
        if parent_id and parent_id in self.nodes:
            self.nodes[parent_id].children.append(node.node_id)
        return node

    def path_to_node(self, node_id: str) -> list[str]:
        """Return sequence of thoughts from root to node."""
        path = []
        current = self.nodes.get(node_id)
        while current:
            path.insert(0, current.thought)
            current = self.nodes.get(current.parent_id) if current.parent_id else None
        return path

    def best_terminal(self) -> Optional[TreeNode]:
        terminals = [n for n in self.nodes.values() if n.is_terminal and not n.is_pruned]
        return max(terminals, key=lambda n: n.score) if terminals else None


def expand_node(
    client: anthropic.Anthropic,
    tree: ThoughtTree,
    node: TreeNode,
    breadth: int = 2,
) -> list[TreeNode]:
    """Generate child thoughts from current node."""
    context = "\n".join(tree.path_to_node(node.node_id))

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": (
            f"Problem: {tree.problem}\n\n"
            f"Reasoning so far:\n{context}\n\n"
            f"Generate {breadth} distinct next reasoning steps. "
            f"Each step should advance the solution differently. "
            f"Format: 'STEP1: ...' 'STEP2: ...'"
        )}],
    )

    text = response.content[0].text
    children = []
    for i in range(1, breadth + 1):
        match = re.search(rf"STEP{i}:\s*(.+?)(?=STEP{i+1}:|$)", text, re.DOTALL)
        if match:
            thought = match.group(1).strip()
            child = tree.new_node(thought, node.depth + 1, node.node_id)
            children.append(child)

    return children


def evaluate_node(
    client: anthropic.Anthropic,
    tree: ThoughtTree,
    node: TreeNode,
) -> tuple[float, bool]:
    """Score node and determine if terminal."""
    context = "\n".join(tree.path_to_node(node.node_id))

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=80,
        messages=[{"role": "user", "content": (
            f"Problem: {tree.problem}\n\nReasoning:\n{context}\n\n"
            "Evaluate: {\"score\": 0.0-1.0, \"is_complete\": true/false, \"is_dead_end\": true/false}"
        )}],
    )

    text = response.content[0].text
    try:
        json_match = re.search(r"\{[^}]+\}", text, re.DOTALL)
        data = __import__("json").loads(json_match.group()) if json_match else {}
        score = float(data.get("score", 0.5))
        is_complete = bool(data.get("is_complete", False))
        is_dead_end = bool(data.get("is_dead_end", False))
        return score, is_complete or is_dead_end
    except Exception:
        return 0.5, False


def depth_first_tot(problem: str, max_depth: int = 3, breadth: int = 2, prune_threshold: float = 0.3) -> str:
    client = anthropic.Anthropic()
    tree = ThoughtTree(problem)

    # Root node
    root = tree.new_node(f"Analyzing: {problem}", depth=0)
    stack = [root]

    while stack:
        node = stack.pop()

        if node.depth >= max_depth:
            node.is_terminal = True
            node.score, _ = evaluate_node(client, tree, node)
            print(f"[ToT] Terminal {node.node_id} depth={node.depth} score={node.score:.2f}")
            continue

        score, is_terminal = evaluate_node(client, tree, node)
        node.score = score

        if score < prune_threshold:
            node.is_pruned = True
            print(f"[ToT] Pruned {node.node_id} (score={score:.2f} < {prune_threshold})")
            continue

        if is_terminal:
            node.is_terminal = True
            print(f"[ToT] Solved at {node.node_id} (score={score:.2f})")
            continue

        # Expand and push children
        children = expand_node(client, tree, node, breadth)
        stack.extend(reversed(children))
        print(f"[ToT] Expanded {node.node_id} → {len(children)} children")

    best = tree.best_terminal()
    if not best:
        return "No solution found within search budget."

    solution_path = "\n".join(tree.path_to_node(best.node_id))

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{"role": "user", "content": (
            f"Problem: {problem}\n\nBest reasoning path found:\n{solution_path}\n\n"
            "Synthesize the final answer."
        )}],
    )
    return response.content[0].text


if __name__ == "__main__":
    problem = "Design an efficient algorithm to find the k-th smallest element in a BST."
    answer = depth_first_tot(problem, max_depth=2, breadth=2)
    print(f"\nAnswer: {answer[:300]}")

# Expected Token Savings: Pruning eliminates poor reasoning paths early, saving 40-60% vs. exploring all branches
# Environment: Algorithm design, mathematical proofs, optimization problems requiring structured exploration
```

## Option 3: Beam Search Over Reasoning Steps

Maintain a beam of the top-K partial reasoning paths, expanding each step in parallel and keeping only the best K.

```python
import anthropic
import asyncio
import re
from dataclasses import dataclass, field


@dataclass
class BeamPath:
    steps: list[str]
    score: float = 1.0

    @property
    def context(self) -> str:
        return "\n".join(f"Step {i+1}: {s}" for i, s in enumerate(self.steps))


async def expand_beam(
    client: anthropic.AsyncAnthropic,
    problem: str,
    path: BeamPath,
) -> list[BeamPath]:
    """Generate 2 next-step candidates for a given path."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": (
            f"Problem: {problem}\n\n"
            f"Reasoning so far:\n{path.context}\n\n"
            "Generate 2 different next reasoning steps. Format:\n"
            "OPTION_A: <step>\nOPTION_B: <step>"
        )}],
    )

    text = response.content[0].text
    new_paths = []
    for label in ["OPTION_A", "OPTION_B"]:
        match = re.search(rf"{label}:\s*(.+?)(?=OPTION_[AB]:|$)", text, re.DOTALL)
        if match:
            new_step = match.group(1).strip()
            new_paths.append(BeamPath(steps=path.steps + [new_step], score=path.score))

    return new_paths


async def score_path(
    client: anthropic.AsyncAnthropic,
    problem: str,
    path: BeamPath,
) -> float:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=60,
        messages=[{"role": "user", "content": (
            f"Problem: {problem}\nReasoning:\n{path.context}\n\n"
            "Score 0.0-1.0 for correctness and promise. Reply: {\"score\": 0.0-1.0}"
        )}],
    )
    try:
        match = re.search(r'"score":\s*([\d.]+)', response.content[0].text)
        return float(match.group(1)) if match else 0.5
    except Exception:
        return 0.5


async def beam_search_tot(
    problem: str,
    beam_width: int = 3,
    max_steps: int = 3,
) -> str:
    client = anthropic.AsyncAnthropic()

    # Initialize beam with one empty path
    beam = [BeamPath(steps=[f"Problem: {problem}"])]

    for step in range(max_steps):
        print(f"[BeamToT] Step {step+1}: expanding {len(beam)} paths...")

        # Expand all paths in parallel
        all_expansions = await asyncio.gather(*[expand_beam(client, problem, p) for p in beam])
        candidates = [path for paths in all_expansions for path in paths]

        # Score all candidates in parallel
        scores = await asyncio.gather(*[score_path(client, problem, c) for c in candidates])
        for path, score in zip(candidates, scores):
            path.score = score

        # Keep top-K by score
        candidates.sort(key=lambda p: -p.score)
        beam = candidates[:beam_width]

        for i, p in enumerate(beam):
            print(f"  Beam[{i}] score={p.score:.2f}: {p.steps[-1][:60]}...")

    # Best path wins
    best = beam[0]
    print(f"\n[BeamToT] Best path score={best.score:.2f}")

    # Final synthesis
    final_response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        messages=[{"role": "user", "content": (
            f"Problem: {problem}\n\nBest reasoning path:\n{best.context}\n\n"
            "Provide the final answer."
        )}],
    )
    return final_response.content[0].text


if __name__ == "__main__":
    problem = "What is the most efficient way to detect cycles in a directed graph?"
    answer = asyncio.run(beam_search_tot(problem, beam_width=3, max_steps=2))
    print(f"\nAnswer: {answer[:300]}")

# Expected Token Savings: Beam search focuses compute on most promising paths; 50-70% more efficient than full tree exploration
# Environment: Complex technical questions, code design decisions, multi-step math problems
```

## Option 4: Self-Evaluation ToT — Model Judges Its Own Branches

Ask the model to both generate and judge branches in a single structured prompt, reducing total API calls.

```python
import anthropic
import json
import re
from dataclasses import dataclass


@dataclass
class EvaluatedBranch:
    approach: str
    reasoning: str
    score: int         # 1-10
    pros: list[str]
    cons: list[str]
    recommended: bool


def self_evaluating_tot(problem: str, n_approaches: int = 3) -> str:
    client = anthropic.Anthropic()

    # Single prompt that generates AND evaluates branches
    generation_prompt = f"""Problem: {problem}

Generate {n_approaches} distinct reasoning approaches, then evaluate each one.

For each approach:
1. Name it
2. Show the step-by-step reasoning
3. Score it 1-10
4. List 2 pros and 2 cons
5. Mark whether you recommend it

Format as JSON array:
[
  {{
    "approach": "approach name",
    "reasoning": "step by step...",
    "score": 8,
    "pros": ["pro1", "pro2"],
    "cons": ["con1", "con2"],
    "recommended": true
  }}
]"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": generation_prompt}],
    )

    text = response.content[0].text

    # Parse branches
    branches = []
    try:
        json_match = re.search(r"\[[\s\S]+\]", text)
        if json_match:
            raw = json.loads(json_match.group())
            for item in raw:
                branches.append(EvaluatedBranch(
                    approach=item.get("approach", ""),
                    reasoning=item.get("reasoning", ""),
                    score=int(item.get("score", 5)),
                    pros=item.get("pros", []),
                    cons=item.get("cons", []),
                    recommended=bool(item.get("recommended", False)),
                ))
    except Exception as e:
        print(f"[ToT] Parse error: {e}")
        # Fall back to raw response
        return text

    if not branches:
        return text

    # Print evaluation
    branches.sort(key=lambda b: -b.score)
    print(f"[Self-Eval ToT] {len(branches)} approaches evaluated:")
    for b in branches:
        flag = "✓" if b.recommended else " "
        print(f"  {flag} {b.approach}: score={b.score}/10")
        print(f"    Pros: {', '.join(b.pros[:2])}")
        print(f"    Cons: {', '.join(b.cons[:2])}")

    # Select best recommended or highest-scored
    best = next((b for b in branches if b.recommended), branches[0])
    print(f"\n[Self-Eval ToT] Selected: {best.approach} (score={best.score})")

    # Synthesize final answer from best branch
    final = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        messages=[{"role": "user", "content": (
            f"Problem: {problem}\n\n"
            f"Best approach: {best.approach}\n\n"
            f"Reasoning: {best.reasoning}\n\n"
            "Provide the final, polished answer."
        )}],
    )
    return final.content[0].text


if __name__ == "__main__":
    problem = "How should a startup prioritize: building new features vs. fixing technical debt?"
    answer = self_evaluating_tot(problem, n_approaches=3)
    print(f"\nFinal Answer:\n{answer[:400]}")

# Expected Token Savings: 50-60% vs. multi-call ToT — generation and evaluation in a single call
# Environment: Decision-making problems, strategic questions, any problem where self-evaluation is reliable
```

## Option 5: Majority Vote ToT — Consensus Across Branches

Generate multiple independent solutions and select the answer that appears most frequently across branches (self-consistency).

```python
import anthropic
import asyncio
from collections import Counter
from dataclasses import dataclass


@dataclass
class VotedSolution:
    answer: str
    vote_count: int
    supporting_reasoning: list[str]
    confidence: float


async def generate_solution(
    client: anthropic.AsyncAnthropic,
    problem: str,
    branch_id: int,
) -> tuple[str, str]:
    """Returns (answer, reasoning)."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=f"You are solver #{branch_id}. Think independently.",
        messages=[{"role": "user", "content": (
            f"Solve this step by step: {problem}\n\n"
            "End your response with: ANSWER: <your final answer>"
        )}],
    )
    text = response.content[0].text
    # Extract final answer
    import re
    match = re.search(r"ANSWER:\s*(.+?)$", text, re.MULTILINE | re.IGNORECASE)
    answer = match.group(1).strip() if match else text[-100:].strip()
    return answer, text


async def majority_vote_tot(
    problem: str,
    n_solutions: int = 5,
) -> VotedSolution:
    client = anthropic.AsyncAnthropic()

    print(f"[MajorityToT] Generating {n_solutions} independent solutions...")

    results = await asyncio.gather(*[
        generate_solution(client, problem, i + 1)
        for i in range(n_solutions)
    ])

    answers = [r[0] for r in results]
    reasonings = [r[1] for r in results]

    # Normalize answers for comparison (lowercase, strip)
    normalized = [a.lower().strip()[:100] for a in answers]
    vote_counts = Counter(normalized)

    # Get most common
    most_common_norm, vote_count = vote_counts.most_common(1)[0]

    # Find original answer text and supporting reasoning
    supporting_idx = [i for i, n in enumerate(normalized) if n == most_common_norm]
    canonical_answer = answers[supporting_idx[0]]
    supporting_reasoning = [reasonings[i][:200] for i in supporting_idx]

    confidence = vote_count / n_solutions

    print(f"[MajorityToT] Vote distribution: {dict(vote_counts.most_common(3))}")
    print(f"[MajorityToT] Winner: {canonical_answer[:60]}... ({vote_count}/{n_solutions} votes, confidence={confidence:.0%})")

    return VotedSolution(
        answer=canonical_answer,
        vote_count=vote_count,
        supporting_reasoning=supporting_reasoning,
        confidence=confidence,
    )


if __name__ == "__main__":
    # Math problem where majority vote improves accuracy
    problem = "If a store offers 20% off an item that costs $85, and then applies an additional 10% off the sale price, what is the final price?"

    result = asyncio.run(majority_vote_tot(problem, n_solutions=5))
    print(f"\nFinal answer: {result.answer}")
    print(f"Confidence: {result.confidence:.0%} ({result.vote_count} of 5 agree)")

# Expected Token Savings: Majority voting reduces correction loops; higher accuracy means fewer follow-up questions
# Environment: Math problems, factual questions, any domain where multiple independent solutions can be compared
```

## Option 6: Hierarchical ToT — Decompose, Branch at Each Level

Decompose the problem into subproblems, apply ToT independently to each, then compose the results.

```python
import anthropic
import json
import re
from dataclasses import dataclass, field


@dataclass
class Subproblem:
    id: int
    description: str
    best_solution: str = ""
    solution_score: float = 0.0


def decompose_problem(client: anthropic.Anthropic, problem: str) -> list[Subproblem]:
    """Break problem into 2-4 independent subproblems."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": (
            f"Break this problem into 2-3 independent subproblems:\n{problem}\n\n"
            'Format: {"subproblems": ["subproblem 1", "subproblem 2", ...]}'
        )}],
    )

    text = response.content[0].text
    try:
        json_match = re.search(r"\{[\s\S]+\}", text)
        data = json.loads(json_match.group()) if json_match else {}
        subs = data.get("subproblems", [])
        return [Subproblem(id=i + 1, description=s) for i, s in enumerate(subs[:4])]
    except Exception:
        return [Subproblem(id=1, description=problem)]


def solve_subproblem_with_tot(
    client: anthropic.Anthropic,
    subproblem: Subproblem,
    n_branches: int = 2,
) -> str:
    """Apply mini-ToT to a single subproblem."""
    candidates = []

    for i in range(n_branches):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=f"Approach #{i+1}.",
            messages=[{"role": "user", "content": f"Solve: {subproblem.description}"}],
        )
        candidates.append(response.content[0].text)

    # Score and pick best
    best_score = -1
    best = candidates[0]
    for candidate in candidates:
        score_response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=40,
            messages=[{"role": "user", "content": (
                f"Rate this solution for '{subproblem.description[:60]}':\n{candidate[:200]}\n\n"
                "Reply: {\"score\": 0.0-1.0}"
            )}],
        )
        try:
            match = re.search(r'"score":\s*([\d.]+)', score_response.content[0].text)
            score = float(match.group(1)) if match else 0.5
        except Exception:
            score = 0.5

        if score > best_score:
            best_score = score
            best = candidate

    return best


def hierarchical_tot(problem: str) -> str:
    client = anthropic.Anthropic()

    print("[HierarchicalToT] Decomposing problem...")
    subproblems = decompose_problem(client, problem)
    print(f"[HierarchicalToT] {len(subproblems)} subproblems identified")

    for sp in subproblems:
        print(f"  Solving sub-{sp.id}: {sp.description[:60]}")
        sp.best_solution = solve_subproblem_with_tot(client, sp, n_branches=2)
        print(f"  Sub-{sp.id} solved: {sp.best_solution[:50]}...")

    # Compose all subproblem solutions
    sub_solutions = "\n\n".join(
        f"Subproblem {sp.id}: {sp.description}\nSolution: {sp.best_solution}"
        for sp in subproblems
    )

    final = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{"role": "user", "content": (
            f"Original problem: {problem}\n\n"
            f"Individual solutions:\n{sub_solutions}\n\n"
            "Integrate all solutions into a complete, cohesive final answer."
        )}],
    )
    return final.content[0].text


if __name__ == "__main__":
    problem = (
        "Design a system that can handle 10,000 concurrent users sending messages, "
        "with guaranteed delivery, message ordering, and sub-100ms latency."
    )
    answer = hierarchical_tot(problem)
    print(f"\nFinal Answer:\n{answer[:500]}")

# Expected Token Savings: Decomposition enables focused ToT on each subproblem; avoids monolithic branch explosion
# Environment: System design questions, multi-component architecture problems, complex engineering decisions
```

## Comparison

| Option | Search Strategy | API Calls | Parallelism | Best For |
|--------|----------------|-----------|-------------|----------|
| 1 Branch-and-Select | N branches → score → best | 2N+1 | None | Simple problems with 3-5 candidate approaches |
| 2 Depth-First with Pruning | DFS + prune bad nodes | Varies | None | Problems needing step-by-step exploration |
| 3 Beam Search | Top-K paths at each step | Parallel | Full async | Problems with many branching decision points |
| 4 Self-Evaluation | Single call generates + scores | 2 total | None | When model's self-judgment is reliable |
| 5 Majority Vote | N independent → vote | N+0 | Full async | Math, factual questions needing accuracy |
| 6 Hierarchical | Decompose → solve each → compose | 3N per sub | Sequential | Complex multi-component system design |
