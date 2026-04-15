---
layout: solution
title: "Agent Oscillates Between Two Solutions — Never Commits"
category: loop-stuck
description: "Agent tries Solution A, decides it's wrong, tries Solution B, decides that's wrong too, reverts to A, and loops indefinitely. Each reversal looks locally reasonable but the agent never makes progress."
tags: [loop-stuck, oscillation, indecision, commit, reversal, progress, planning]
---

# Agent Oscillates Between Two Solutions — Never Commits

## Symptom
- Agent alternates between two approaches every 2-3 turns with no convergence
- Each reversal is accompanied by plausible-sounding reasoning
- Agent undoes its own work from 3 turns ago
- Progress counter stays at the same step after 20 turns
- Agent changes a config value, then changes it back, then changes it again
- "I think the issue is X" → fix X → "Actually it's Y" → fix Y → "Actually it was X after all"

## Root Cause
Without memory of previous attempts and their outcomes, the agent re-evaluates each option from scratch each turn. Both options have surface-level plausibility. Without a structured record showing that A was already tried and failed, the agent's reasoning leads it back to A. The oscillation is often caused by: evaluating options in isolation, forgetting the failure mode of the previously tried option, or having no explicit commitment mechanism.

## Fix

### Option 1: Attempt log — record what was tried and why it failed

```python
from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class Attempt:
    approach: str
    action_taken: str
    outcome: str
    failed_because: str
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())

class AttemptLog:
    """Track every approach tried so the agent doesn't re-try failed paths"""

    def __init__(self):
        self._attempts: list[Attempt] = []

    def record(self, approach: str, action: str, outcome: str, failed_because: str = ""):
        self._attempts.append(Attempt(approach, action, outcome, failed_because))

    def already_tried(self, approach: str) -> Attempt | None:
        """Check if this approach was already tried"""
        for a in self._attempts:
            if approach.lower() in a.approach.lower():
                return a
        return None

    def as_context(self) -> str:
        """Format as context string to inject into next agent turn"""
        if not self._attempts:
            return "No approaches tried yet."
        lines = ["Previously tried approaches (DO NOT retry these):"]
        for i, a in enumerate(self._attempts, 1):
            lines.append(f"{i}. {a.approach}: {a.outcome}")
            if a.failed_because:
                lines.append(f"   Failed because: {a.failed_because}")
        return "\n".join(lines)

log = AttemptLog()

# After each agent attempt, record the outcome:
log.record(
    approach="Increase timeout to 60s",
    action="Set REQUEST_TIMEOUT=60 in config",
    outcome="Failed — still timing out at 30s",
    failed_because="The timeout is enforced by the upstream load balancer, not our config"
)

# Next turn — inject the log into the prompt:
system_addition = f"\n\n{log.as_context()}"
# → Agent sees: "Previously tried: Increase timeout — Failed because upstream LB controls it"
# → Agent will not suggest increasing timeout again
```

### Option 2: Explicit commitment protocol

```python
COMMITMENT_PROMPT = """
Decision protocol:
1. List all candidate approaches (A, B, C...)
2. For each, state: known pros, known cons, why it might fail
3. Select ONE approach and state: "I am committing to [approach] because [reason]"
4. Execute the committed approach fully before evaluating alternatives
5. Only switch if you have NEW information that wasn't available when you committed
6. If you find yourself reconsidering: state explicitly what NEW information changed your assessment

DO NOT switch approaches based on the same reasoning you already considered.
"Reconsidering" without new evidence is a sign of oscillation — stop and report instead.
"""

async def solve_with_commitment(problem: str, agent, max_iterations: int = 5) -> str:
    history = [{"role": "user", "content": f"{COMMITMENT_PROMPT}\n\nProblem: {problem}"}]
    committed_approach = None

    for iteration in range(max_iterations):
        response = await agent.call(history)
        history.append({"role": "assistant", "content": response})

        # Detect commitment
        if "I am committing to" in response and not committed_approach:
            committed_approach = response
            print(f"Iteration {iteration}: Approach committed")

        # Detect oscillation: switching away from committed approach
        if committed_approach and "actually" in response.lower() and "new information" not in response.lower():
            history.append({
                "role": "user",
                "content": (
                    "You appear to be switching approaches without new information. "
                    "What NEW evidence changed your assessment since you committed? "
                    "If there is no new evidence, continue with the committed approach."
                )
            })
            continue

        # Check for completion
        if any(phrase in response.lower() for phrase in ["completed", "resolved", "done", "fixed"]):
            return response

        history.append({"role": "user", "content": "Continue with the next step."})

    return f"Max iterations reached. Last state: {history[-1]['content'][:200]}"
```

### Option 3: Detect oscillation automatically

```python
from collections import Counter
import re

def detect_oscillation(history: list, window: int = 6) -> bool:
    """
    Detect if the agent is oscillating by checking if recent actions
    repeat the same operations in alternating sequence.
    """
    # Extract action phrases from recent assistant turns
    recent_actions = []
    assistant_turns = [
        m["content"] for m in history[-window:]
        if m["role"] == "assistant"
    ]

    # Look for repeated reversal keywords
    reversal_patterns = [
        r"revert(ing|ed)?\s+to",
        r"actually.{0,30}(should|is|was)",
        r"undoing",
        r"going back to",
        r"switch(ing)?\s+(back|to)",
        r"reconsidering",
    ]

    reversal_count = sum(
        1 for turn in assistant_turns
        for pattern in reversal_patterns
        if re.search(pattern, turn, re.IGNORECASE)
    )

    # If >2 reversals in last 6 turns, oscillation detected
    return reversal_count >= 2

async def monitored_agent_loop(problem: str, agent) -> str:
    history = [{"role": "user", "content": problem}]

    for turn in range(20):
        response = await agent.call(history)
        history.append({"role": "assistant", "content": response})

        if detect_oscillation(history):
            print(f"Oscillation detected at turn {turn} — injecting circuit breaker")
            history.append({
                "role": "user",
                "content": (
                    "STOP. You are oscillating between approaches. "
                    "Pick ONE approach, state it explicitly, and execute it to completion. "
                    "Do not switch again unless you receive new external information. "
                    "If you cannot determine which approach is better, choose the simpler one."
                )
            })

        if "done" in response.lower() or "resolved" in response.lower():
            break

    return history[-1]["content"]
```

### Option 4: Force decision tree with explicit branching

```python
DECISION_TREE_PROMPT = """
When solving a problem with multiple possible approaches:

1. ENUMERATE: List all approaches you're considering (max 3)
2. SCORE: Rate each on (1-5): likelihood of success, implementation cost, reversibility
3. SELECT: Choose the highest-scoring approach
4. LOCK: State "LOCKED ON: [approach]" — do not change this
5. EXECUTE: Implement fully
6. EVALUATE: Did it work? Yes → done. No → record failure reason
7. ESCALATE: If the locked approach failed, report to user with full context

You are NOT allowed to switch from the locked approach mid-execution.
You ARE allowed to request a human decision if both approaches seem equally valid.
"""

# Structured scoring to prevent gut-feel oscillation:
def score_approaches(approaches: list[dict]) -> dict:
    """
    approaches: [{"name": str, "success_likelihood": int, "cost": int, "reversible": bool}]
    Returns the highest-scoring approach.
    """
    for a in approaches:
        a["score"] = (
            a["success_likelihood"] * 2 +     # Weight success most
            (6 - a["cost"]) +                 # Lower cost is better
            (2 if a["reversible"] else 0)     # Prefer reversible
        )
    return max(approaches, key=lambda x: x["score"])

best = score_approaches([
    {"name": "Approach A", "success_likelihood": 3, "cost": 2, "reversible": True},
    {"name": "Approach B", "success_likelihood": 4, "cost": 4, "reversible": False},
])
# → Selects deterministically, no oscillation possible
```

### Option 5: Human-in-the-loop for genuine ambiguity

```python
async def solve_with_escalation(problem: str, agent, max_turns: int = 8) -> str:
    """
    Let agent attempt solution; escalate to human if it can't converge.
    """
    history = [{"role": "user", "content": problem}]
    oscillation_count = 0

    for turn in range(max_turns):
        response = await agent.call(history)
        history.append({"role": "assistant", "content": response})

        if detect_oscillation(history):
            oscillation_count += 1

        if oscillation_count >= 2:
            # Extract the two approaches the agent is oscillating between
            summary = await agent.call([{
                "role": "user",
                "content": (
                    f"You have been oscillating. Summarize in one sentence each:\n"
                    f"- What is Approach A and its risk?\n"
                    f"- What is Approach B and its risk?\n"
                    f"Be specific.\n\nHistory:\n" +
                    "\n".join(m["content"][:100] for m in history[-6:])
                )
            }])
            raise NeedsHumanDecision(
                f"Agent oscillating after {turn} turns. Two approaches identified:\n{summary}\n"
                f"Please choose one or provide additional constraints."
            )

        if "resolved" in response.lower():
            return response

        history.append({"role": "user", "content": "Continue."})

    return "Max turns reached without resolution."

class NeedsHumanDecision(Exception):
    pass
```

## Oscillation vs. Legitimate Reconsideration

| Signal | Oscillation | Legitimate reconsideration |
|---|---|---|
| Reason for switch | Same reasoning as before | New external evidence |
| Pattern | A→B→A→B | A→B (with explicit reason) |
| New information | None | Test result, tool output, user input |
| Action | Undo previous work | Build on previous work |
| Frequency | Every 2-3 turns | Once per session |

## Expected Token Savings
20-turn oscillation loop without resolution: ~40,000 tokens
Commitment protocol resolves in 4-5 turns: ~8,000 tokens

## Environment
- Agents doing iterative debugging, configuration tuning, or multi-option problem solving
- Source: direct experience; oscillation is the second most common form of agent loop after infinite validation
