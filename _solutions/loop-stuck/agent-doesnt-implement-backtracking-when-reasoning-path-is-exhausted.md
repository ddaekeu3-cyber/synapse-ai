---
layout: solution
title: "Agent Doesn't Implement Backtracking When Reasoning Path Is Exhausted"
category: loop-stuck
description: "Detect when a reasoning or tool-use path has reached a dead end and backtrack to a previous decision point to try an alternative approach, preventing infinite retries on the same failed path."
tags: [loop-stuck, backtracking, reasoning, dead-end-detection, tree-search, exploration, recovery]
---

# Agent Doesn't Implement Backtracking When Reasoning Path Is Exhausted

## Problem

An agent committed to one approach — a specific tool call sequence, a particular search query pattern, or a single reasoning chain — keeps retrying the same dead-end path when it fails. Without backtracking, the agent either loops until it hits a budget limit or exits with an unhelpful error. Backtracking means recognizing a dead end, discarding the failed path, and resuming from the last viable decision point with a different strategy.

## Solution Options

### Option 1: Simple Dead-End Detection with Strategy Rotation

```python
import anthropic
from enum import Enum


class Strategy(Enum):
    DIRECT = "direct"
    STEP_BY_STEP = "step_by_step"
    ANALOGY = "analogy"
    COUNTEREXAMPLE = "counterexample"


STRATEGY_PROMPTS = {
    Strategy.DIRECT:          "Answer directly and concisely.",
    Strategy.STEP_BY_STEP:    "Break the problem into numbered steps before answering.",
    Strategy.ANALOGY:         "Use a real-world analogy to explain your answer.",
    Strategy.COUNTEREXAMPLE:  "Start by describing what the answer is NOT, then give the correct answer.",
}

DEAD_END_SIGNALS = [
    "i don't know",
    "i cannot",
    "i'm unable",
    "i am unable",
    "i cannot determine",
    "not enough information",
    "unclear",
]


def is_dead_end(response: str) -> bool:
    lower = response.lower()
    return any(signal in lower for signal in DEAD_END_SIGNALS)


def solve_with_backtracking(question: str) -> str:
    client = anthropic.Anthropic()
    strategies = list(Strategy)

    for i, strategy in enumerate(strategies):
        prompt = STRATEGY_PROMPTS[strategy]
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=f"You are a problem-solver. {prompt}",
            messages=[{"role": "user", "content": question}],
        )
        answer = resp.content[0].text.strip()

        if not is_dead_end(answer):
            print(f"[backtrack] Strategy '{strategy.value}' succeeded on attempt {i + 1}")
            return answer

        print(f"[backtrack] Strategy '{strategy.value}' hit dead end — backtracking")

    # All strategies exhausted — return best available
    return f"All {len(strategies)} strategies exhausted. Last answer: {answer}"


if __name__ == "__main__":
    # Normal question
    print(solve_with_backtracking("What is the capital of France?"))

    # Harder question that may need backtracking
    print(solve_with_backtracking("Why does water freeze from the top down in lakes?"))

# Expected Token Savings: Stop retrying failed paths early; skip to next strategy immediately
# Environment: Agents with multiple solution strategies for open-ended reasoning tasks
```

---

### Option 2: Tool-Use Backtracking with Path Memory

```python
import anthropic
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DecisionNode:
    node_id: int
    action: str
    result: str | None = None
    failed: bool = False
    children: list["DecisionNode"] = field(default_factory=list)
    alternatives_tried: list[str] = field(default_factory=list)


class BacktrackingAgent:
    """
    Maintains a decision tree. When a tool call returns a dead-end result,
    the agent backtracks to the parent node and tries an alternative action.
    """

    MAX_DEPTH = 4
    MAX_BACKTRACKS = 6

    def __init__(self) -> None:
        self._client = anthropic.Anthropic()
        self._node_counter = 0
        self._backtracks = 0
        self._path: list[DecisionNode] = []

    def _new_node(self, action: str) -> DecisionNode:
        self._node_counter += 1
        return DecisionNode(node_id=self._node_counter, action=action)

    def _call_llm(self, messages: list[dict]) -> str:
        resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=(
                "You are a research agent. For each step, propose ONE specific action "
                "from: [search, calculate, summarize, verify, conclude]. "
                "Format: ACTION: <action> | QUERY: <what to do>"
            ),
            messages=messages,
        )
        return resp.content[0].text.strip()

    def _simulate_tool(self, action: str, query: str) -> tuple[str, bool]:
        """Simulate tool execution. Returns (result, is_dead_end)."""
        # Simulate some actions failing
        dead_end_queries = {"search for secret", "calculate impossible", "verify unknown"}
        if any(d in query.lower() for d in dead_end_queries):
            return "No results found. This path is exhausted.", True
        return f"Tool '{action}' returned: result for '{query[:30]}'", False

    def _is_dead_end(self, result: str) -> bool:
        signals = ["no results", "exhausted", "cannot find", "not available", "failed"]
        return any(s in result.lower() for s in signals)

    def run(self, goal: str) -> str:
        messages = [{"role": "user", "content": f"Goal: {goal}\nStep 1: what action should I take?"}]
        tried_at_depth: dict[int, list[str]] = {}

        for step in range(self.MAX_DEPTH * 2):
            depth = len(self._path)
            response = self._call_llm(messages)

            # Parse action
            action, query = "search", goal
            if "ACTION:" in response and "QUERY:" in response:
                parts = response.split("|")
                action = parts[0].split("ACTION:")[1].strip()
                query = parts[1].split("QUERY:")[1].strip() if len(parts) > 1 else goal

            action_key = f"{action}:{query[:20]}"
            tried_at_depth.setdefault(depth, [])

            # Check if we've already tried this at this depth
            if action_key in tried_at_depth[depth]:
                print(f"[backtrack] Duplicate action at depth {depth} — forcing backtrack")
                if self._path and self._backtracks < self.MAX_BACKTRACKS:
                    self._backtracks += 1
                    node = self._path.pop()
                    node.failed = True
                    messages.append({"role": "assistant", "content": response})
                    messages.append({"role": "user", "content": f"That path failed. Try a completely different approach for: {goal}"})
                    continue
                break

            tried_at_depth[depth].append(action_key)
            result, is_simulated_dead_end = self._simulate_tool(action, query)

            node = self._new_node(action)
            node.result = result

            if is_simulated_dead_end or self._is_dead_end(result):
                node.failed = True
                self._backtracks += 1
                print(f"[backtrack] Dead end at depth {depth}: '{action}' → backtracking ({self._backtracks}/{self.MAX_BACKTRACKS})")
                messages.append({"role": "assistant", "content": response})
                messages.append({"role": "user", "content": f"Dead end: {result}. Backtrack and try a different approach."})
                if self._path:
                    self._path.pop()
                continue

            self._path.append(node)
            messages.append({"role": "assistant", "content": response})
            messages.append({"role": "user", "content": f"Tool result: {result}\nContinue or conclude?"})

            if "conclude" in response.lower() or depth >= self.MAX_DEPTH - 1:
                break

        # Final answer
        final_resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=messages + [{"role": "user", "content": f"Summarize findings for goal: {goal}"}],
        )
        print(f"[backtrack] Completed with {self._backtracks} backtracks, {len(self._path)} successful nodes")
        return final_resp.content[0].text.strip()


if __name__ == "__main__":
    agent = BacktrackingAgent()
    result = agent.run("Find information about renewable energy trends")
    print(f"\nFinal answer: {result}")

# Expected Token Savings: Early exit from dead paths; max_backtracks cap prevents runaway retries
# Environment: Tool-use agents with multiple action types and fallible external tools
```

---

### Option 3: Search Tree with Beam-Width Backtracking

```python
import anthropic
from dataclasses import dataclass, field
import heapq


@dataclass(order=True)
class SearchNode:
    score: float          # higher = more promising (heap uses min, so negate)
    depth: int = field(compare=False)
    hypothesis: str = field(compare=False)
    parent_hypothesis: str | None = field(default=None, compare=False)
    visited: bool = field(default=False, compare=False)


class BeamBacktrackingAgent:
    """
    Maintains a priority queue of hypotheses.
    When the top hypothesis fails, the agent backtracks to the next best hypothesis.
    Implements beam search with width=3: keep top-3 hypotheses alive at each depth.
    """

    BEAM_WIDTH = 3
    MAX_DEPTH = 4

    def __init__(self) -> None:
        self._client = anthropic.Anthropic()

    def _score_hypothesis(self, hypothesis: str, goal: str) -> float:
        resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=20,
            messages=[{
                "role": "user",
                "content": (
                    f"Goal: {goal}\nHypothesis: {hypothesis}\n"
                    f"Score how promising this hypothesis is (0.0 to 1.0). "
                    f"Respond with a single decimal number only."
                ),
            }],
        )
        try:
            return float(resp.content[0].text.strip())
        except ValueError:
            return 0.5

    def _expand(self, hypothesis: str, goal: str) -> list[str]:
        resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    f"Goal: {goal}\nCurrent hypothesis: {hypothesis}\n"
                    f"Generate exactly 3 different follow-up hypotheses to explore. "
                    f"One per line, no numbering."
                ),
            }],
        )
        lines = [l.strip() for l in resp.content[0].text.strip().splitlines() if l.strip()]
        return lines[:3]

    def _is_answer(self, hypothesis: str) -> bool:
        signals = ["therefore", "in conclusion", "the answer is", "this proves", "thus"]
        return any(s in hypothesis.lower() for s in signals)

    def solve(self, goal: str) -> str:
        heap: list[SearchNode] = []
        # Seed with initial hypothesis
        initial = SearchNode(score=-0.5, depth=0, hypothesis=f"Investigate: {goal}")
        heapq.heappush(heap, initial)

        visited_hypotheses: set[str] = set()
        best_answer: str | None = None
        steps = 0

        while heap and steps < 20:
            node = heapq.heappop(heap)
            if node.hypothesis in visited_hypotheses:
                continue
            visited_hypotheses.add(node.hypothesis)
            steps += 1

            print(f"[beam] depth={node.depth} score={-node.score:.2f} → {node.hypothesis[:60]}")

            if self._is_answer(node.hypothesis) or node.depth >= self.MAX_DEPTH:
                best_answer = node.hypothesis
                break

            # Expand to children
            children = self._expand(node.hypothesis, goal)
            for child in children:
                if child in visited_hypotheses:
                    continue
                score = self._score_hypothesis(child, goal)
                child_node = SearchNode(
                    score=-score,  # negate for min-heap
                    depth=node.depth + 1,
                    hypothesis=child,
                    parent_hypothesis=node.hypothesis,
                )
                heapq.heappush(heap, child_node)

            # Trim beam: keep only top BEAM_WIDTH candidates
            if len(heap) > self.BEAM_WIDTH * 2:
                heap = heapq.nsmallest(self.BEAM_WIDTH, heap)
                heapq.heapify(heap)

        if not best_answer:
            best_answer = f"Beam search exhausted after {steps} steps. Best: {node.hypothesis}"

        # Synthesize final answer
        resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{
                "role": "user",
                "content": f"Based on hypothesis '{best_answer}', give a direct answer to: {goal}",
            }],
        )
        return resp.content[0].text.strip()


if __name__ == "__main__":
    agent = BeamBacktrackingAgent()
    answer = agent.solve("How does photosynthesis produce oxygen?")
    print(f"\nFinal answer: {answer}")

# Expected Token Savings: Beam pruning discards low-score branches early; avoids deep dead-end exploration
# Environment: Open-ended research tasks requiring hypothesis generation and evaluation
```

---

### Option 4: Checkpoint-Based Backtracking with State Restore

```python
import anthropic
import copy
from dataclasses import dataclass, field


@dataclass
class AgentCheckpoint:
    checkpoint_id: int
    conversation: list[dict]
    decision: str
    depth: int
    alternatives: list[str] = field(default_factory=list)


class CheckpointBacktracker:
    """
    Saves checkpoints at each decision point.
    On dead end, restores the nearest checkpoint and tries the next alternative.
    """

    MAX_CHECKPOINTS = 5
    MAX_DEPTH = 6

    def __init__(self) -> None:
        self._client = anthropic.Anthropic()
        self._checkpoints: list[AgentCheckpoint] = []
        self._cp_id = 0

    def _save_checkpoint(self, conv: list[dict], decision: str, depth: int, alternatives: list[str]) -> AgentCheckpoint:
        cp = AgentCheckpoint(
            checkpoint_id=self._cp_id,
            conversation=copy.deepcopy(conv),
            decision=decision,
            depth=depth,
            alternatives=alternatives[:],
        )
        self._cp_id += 1
        self._checkpoints.append(cp)
        if len(self._checkpoints) > self.MAX_CHECKPOINTS:
            self._checkpoints.pop(0)
        return cp

    def _restore_checkpoint(self) -> AgentCheckpoint | None:
        while self._checkpoints:
            cp = self._checkpoints[-1]
            if cp.alternatives:
                return cp
            self._checkpoints.pop()
        return None

    def _propose_alternatives(self, conv: list[dict], current_decision: str) -> list[str]:
        resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=conv + [{
                "role": "user",
                "content": (
                    f"Current approach: {current_decision}\n"
                    f"This didn't work. List 2 alternative approaches, one per line."
                ),
            }],
        )
        lines = [l.strip() for l in resp.content[0].text.splitlines() if l.strip()]
        return lines[:2]

    def _is_stuck(self, response: str) -> bool:
        stuck = ["cannot proceed", "dead end", "no way to", "impossible", "not possible", "blocked"]
        return any(s in response.lower() for s in stuck)

    def _is_complete(self, response: str) -> bool:
        done = ["final answer:", "conclusion:", "therefore,", "in summary,", "the answer is"]
        return any(s in response.lower() for s in done)

    def run(self, task: str) -> str:
        conv: list[dict] = [{"role": "user", "content": f"Task: {task}. Begin step-by-step reasoning."}]
        total_backtracks = 0

        for step in range(self.MAX_DEPTH * 3):
            resp = self._client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=conv,
            )
            response = resp.content[0].text.strip()
            conv.append({"role": "assistant", "content": response})

            if self._is_complete(response):
                print(f"[checkpoint] Completed in {step + 1} steps, {total_backtracks} backtracks")
                return response

            if self._is_stuck(response):
                print(f"[checkpoint] Stuck at step {step + 1} — attempting backtrack")
                # Generate alternatives before backtracking
                alts = self._propose_alternatives(conv, response)
                self._save_checkpoint(conv[:-1], response, step, alts)

                # Restore and try next alternative
                cp = self._restore_checkpoint()
                if cp is None:
                    print("[checkpoint] No checkpoint available — giving up")
                    break

                alt = cp.alternatives.pop(0)
                conv = copy.deepcopy(cp.conversation)
                conv.append({"role": "user", "content": f"Previous approach failed. Try this instead: {alt}"})
                total_backtracks += 1
                print(f"[checkpoint] Restored to checkpoint {cp.checkpoint_id}, trying: {alt[:50]}")
                continue

            # Progress — save checkpoint for potential future backtrack
            if step % 2 == 0:
                alts = self._propose_alternatives(conv, response)
                self._save_checkpoint(conv, response, step, alts)

            conv.append({"role": "user", "content": "Continue reasoning."})

        return f"Task incomplete after {total_backtracks} backtracks. Last: {response[:100]}"


if __name__ == "__main__":
    agent = CheckpointBacktracker()
    result = agent.run("Calculate the optimal route for visiting 4 cities minimizing total distance")
    print(f"\nResult: {result[:200]}")

# Expected Token Savings: Checkpoints avoid re-computing successful steps; only re-explore from failure point
# Environment: Multi-step planning or optimization agents with reversible decision points
```

---

### Option 5: Async Parallel Exploration with Backtrack on Convergence Failure

```python
import anthropic
import asyncio
from dataclasses import dataclass


@dataclass
class ExplorationBranch:
    branch_id: str
    approach: str
    result: str | None = None
    failed: bool = False
    confidence: float = 0.0


class AsyncBacktrackingExplorer:
    """
    Explores multiple reasoning branches in parallel.
    If no branch succeeds, backtracks by generating new branches from failure context.
    """

    NUM_PARALLEL_BRANCHES = 3
    MAX_BACKTRACK_ROUNDS = 3

    def __init__(self) -> None:
        self._client = anthropic.AsyncAnthropic()

    async def _generate_approaches(self, task: str, failed_approaches: list[str]) -> list[str]:
        exclusion = (
            f"\nDo NOT use these failed approaches: {failed_approaches}"
            if failed_approaches else ""
        )
        resp = await self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    f"Task: {task}{exclusion}\n"
                    f"Generate exactly {self.NUM_PARALLEL_BRANCHES} distinct approaches. One per line."
                ),
            }],
        )
        lines = [l.strip() for l in resp.content[0].text.splitlines() if l.strip()]
        return lines[:self.NUM_PARALLEL_BRANCHES]

    async def _execute_branch(self, branch: ExplorationBranch, task: str) -> ExplorationBranch:
        resp = await self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": f"Task: {task}\nApproach: {branch.approach}\nExecute this approach and give your answer.",
            }],
        )
        result = resp.content[0].text.strip()
        # Score confidence
        score_resp = await self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{
                "role": "user",
                "content": f"How confident are you in this answer (0.0-1.0): {result[:100]}\nRespond with one decimal number only.",
            }],
        )
        try:
            confidence = float(score_resp.content[0].text.strip())
        except ValueError:
            confidence = 0.5

        failed = confidence < 0.4 or any(
            s in result.lower() for s in ["cannot", "impossible", "don't know", "unclear"]
        )
        branch.result = result
        branch.failed = failed
        branch.confidence = confidence
        return branch

    async def solve(self, task: str) -> str:
        failed_approaches: list[str] = []

        for round_num in range(self.MAX_BACKTRACK_ROUNDS):
            print(f"[async-backtrack] Round {round_num + 1}: generating {self.NUM_PARALLEL_BRANCHES} branches")
            approaches = await self._generate_approaches(task, failed_approaches)
            branches = [
                ExplorationBranch(branch_id=f"r{round_num}-b{i}", approach=approach)
                for i, approach in enumerate(approaches)
            ]

            # Execute all branches in parallel
            results = await asyncio.gather(
                *[self._execute_branch(b, task) for b in branches],
                return_exceptions=True,
            )
            branches = [r for r in results if isinstance(r, ExplorationBranch)]

            # Find best non-failed branch
            successful = [b for b in branches if not b.failed]
            if successful:
                best = max(successful, key=lambda b: b.confidence)
                print(f"[async-backtrack] Branch {best.branch_id} succeeded (confidence={best.confidence:.2f})")
                return best.result or ""

            # All failed — collect failures and backtrack
            for b in branches:
                failed_approaches.append(b.approach[:50])
            print(f"[async-backtrack] All branches failed in round {round_num + 1} — backtracking")

        return f"All {self.MAX_BACKTRACK_ROUNDS} backtrack rounds exhausted."

    async def close(self) -> None:
        await self._client.close()


async def main() -> None:
    explorer = AsyncBacktrackingExplorer()
    result = await explorer.solve("Design a sorting algorithm that works without comparisons")
    print(f"\nFinal answer: {result[:200]}")
    await explorer.close()


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Parallel branches amortize exploration cost; best branch wins immediately
# Environment: Open-ended problem solving where the optimal approach is unknown in advance
```

---

### Option 6: Budget-Bounded Backtracking with Partial Credit

```python
import anthropic
from dataclasses import dataclass, field


@dataclass
class AttemptRecord:
    attempt_id: int
    approach: str
    result: str
    tokens_used: int
    partial_credit: float  # 0.0–1.0, how close to correct


class BudgetedBacktracker:
    """
    Backtracks within a fixed token budget.
    Tracks partial credit per attempt so the final synthesis can
    combine the best elements of all approaches even when none fully succeeded.
    """

    TOKEN_BUDGET = 5000
    MIN_PARTIAL_CREDIT_TO_KEEP = 0.3

    def __init__(self) -> None:
        self._client = anthropic.Anthropic()
        self._tokens_spent = 0
        self._attempts: list[AttemptRecord] = []

    def _tokens_remaining(self) -> int:
        return self.TOKEN_BUDGET - self._tokens_spent

    def _call(self, messages: list[dict], max_tokens: int = 256) -> tuple[str, int]:
        max_tokens = min(max_tokens, self._tokens_remaining())
        if max_tokens < 10:
            return "BUDGET_EXHAUSTED", 0
        resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_tokens,
            messages=messages,
        )
        used = resp.usage.input_tokens + resp.usage.output_tokens
        self._tokens_spent += used
        return resp.content[0].text.strip(), used

    def _score_partial(self, result: str, goal: str) -> float:
        if self._tokens_remaining() < 50:
            return 0.5  # can't afford to score
        score_text, used = self._call([{
            "role": "user",
            "content": f"Goal: {goal}\nResult: {result[:200]}\nRate completeness 0.0-1.0. Reply with one number only.",
        }], max_tokens=10)
        try:
            return float(score_text)
        except ValueError:
            return 0.3

    def _next_approach(self, goal: str, failed: list[str]) -> str:
        exclusion = f"Do not use: {failed}" if failed else ""
        text, _ = self._call([{
            "role": "user",
            "content": f"Goal: {goal}. {exclusion}\nPropose one new approach in 1 sentence.",
        }], max_tokens=60)
        return text

    def solve(self, goal: str) -> str:
        failed_approaches: list[str] = []
        attempt_id = 0

        while self._tokens_remaining() > 200:
            approach = self._next_approach(goal, failed_approaches)
            result, tokens = self._call([{
                "role": "user",
                "content": f"Approach: {approach}\nTask: {goal}\nGive your best answer.",
            }])

            if result == "BUDGET_EXHAUSTED":
                break

            partial = self._score_partial(result, goal)
            record = AttemptRecord(attempt_id, approach, result, tokens, partial)
            self._attempts.append(record)
            attempt_id += 1

            print(f"[budget-bt] Attempt {attempt_id}: partial={partial:.2f} tokens={tokens} "
                  f"remaining={self._tokens_remaining()}")

            if partial >= 0.85:
                print("[budget-bt] High confidence answer found — stopping")
                return result

            if partial < self.MIN_PARTIAL_CREDIT_TO_KEEP:
                failed_approaches.append(approach[:40])

        # Synthesize from best partial attempts
        good_attempts = sorted(
            [a for a in self._attempts if a.partial_credit >= self.MIN_PARTIAL_CREDIT_TO_KEEP],
            key=lambda a: a.partial_credit,
            reverse=True,
        )[:3]

        if not good_attempts:
            return f"No viable attempt found. Spent {self._tokens_spent} tokens."

        combined = "\n\n".join(
            f"Attempt {a.attempt_id} (score={a.partial_credit:.2f}):\n{a.result[:150]}"
            for a in good_attempts
        )
        synthesis, _ = self._call([{
            "role": "user",
            "content": f"Goal: {goal}\n\nBest attempts:\n{combined}\n\nSynthesize into a final answer.",
        }])
        print(f"[budget-bt] Synthesized from {len(good_attempts)} attempts. Total tokens: {self._tokens_spent}")
        return synthesis


if __name__ == "__main__":
    bt = BudgetedBacktracker()
    answer = bt.solve("Explain how quantum entanglement could be used for communication")
    print(f"\nFinal answer: {answer[:300]}")

# Expected Token Savings: Hard token budget cap; partial credit synthesis avoids discarding useful work
# Environment: Agents with strict cost limits that still need best-effort answers
```

---

## Comparison

| Option | Approach | Best For | Backtrack Trigger | Budget Awareness |
|--------|----------|----------|-------------------|-----------------|
| 1 | Strategy rotation on dead-end signals | Fixed set of known strategies | String pattern detection | None |
| 2 | Tool-use path tree with visited tracking | Tool-calling agents with multiple actions | Tool result dead-end | Max backtracks cap |
| 3 | Beam search with priority queue | Open-ended hypothesis exploration | Low score pruning | Beam width limit |
| 4 | Checkpoint + state restore | Multi-step planning with reversible state | Stuck signal detection | Max checkpoint depth |
| 5 | Async parallel branches + convergence | Unknown optimal approach, parallel budget | Confidence threshold | Parallel branch count |
| 6 | Budget-bounded with partial credit synthesis | Strict cost limits, best-effort answers | Low partial credit | Hard token budget |
