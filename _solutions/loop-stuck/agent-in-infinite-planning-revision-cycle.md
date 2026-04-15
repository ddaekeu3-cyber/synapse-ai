---
layout: solution
title: "Agent Revises Plan Endlessly Without Executing — Infinite Planning Loop"
category: loop-stuck
description: "Agent creates a plan, finds a flaw, revises the plan, finds another issue, revises again. Never moves to execution. Planning loop continues indefinitely consuming tokens without producing output."
tags: [planning, loop, revision, stuck, execution, over-planning]
---

# Agent Revises Plan Endlessly Without Executing — Infinite Planning Loop

## Symptom
- Agent produces 5 versions of a plan but never starts executing
- Each plan revision adds one more consideration, then needs another revision
- Agent says "let me refine the approach" repeatedly
- Hours of planning, zero output produced
- Agent discovers edge case → revises plan → discovers another edge case → revises
- "Before I start, I want to make sure the plan is complete..."

## Root Cause
No explicit transition from planning to execution. Without a clear stopping criterion, the model can always find one more thing to consider. Planning feels "safe" — it's hard to make a wrong plan, but executing means potentially making mistakes. The model can be pulled into an indefinite refinement loop.

## Fix

### Option 1: Hard limit on planning iterations

```
System prompt:
"Planning rules:
- Maximum 2 planning steps before execution
- Plan 1: Outline the approach in bullet points
- Plan 2 (optional): One revision if a critical issue is found
- After plan 2: BEGIN EXECUTION — no more planning
- If you discover an issue during execution, handle it in place — don't restart planning
- A good-enough plan executed now beats a perfect plan never executed"
```

### Option 2: Explicit plan → execute state machine

```python
from enum import Enum

class AgentState(Enum):
    PLANNING = "planning"
    EXECUTING = "executing"
    DONE = "done"

class PlanExecuteAgent:
    def __init__(self, max_plan_turns: int = 2):
        self.state = AgentState.PLANNING
        self.plan_turns = 0
        self.max_plan_turns = max_plan_turns
        self.current_plan = None

    async def step(self, agent, message: str) -> str:
        if self.state == AgentState.PLANNING:
            self.plan_turns += 1
            response = await agent.complete(
                f"{message}\n\nYou are in PLANNING mode. Turn {self.plan_turns}/{self.max_plan_turns}."
            )
            self.current_plan = response

            if self.plan_turns >= self.max_plan_turns:
                self.state = AgentState.EXECUTING
                print(f"Switching to EXECUTING mode after {self.plan_turns} planning turns")

            return response

        elif self.state == AgentState.EXECUTING:
            response = await agent.complete(
                f"EXECUTING the plan: {self.current_plan}\n\nDo the actual work now. No more planning."
            )
            return response
```

### Option 3: Time-box planning

```python
import asyncio, time

async def plan_with_timeout(task: str, agent, planning_budget_seconds: int = 30) -> str:
    """Force transition to execution after time limit"""
    plan_start = time.time()

    response = await agent.complete(
        f"Create a brief plan for: {task}\n"
        f"You have {planning_budget_seconds} seconds to plan, then you must execute."
    )

    elapsed = time.time() - plan_start
    if elapsed >= planning_budget_seconds:
        print(f"Planning budget exhausted ({elapsed:.0f}s). Forcing execution.")
        response = await agent.complete(
            f"STOP PLANNING. Execute the best plan you have now: {response}"
        )

    return response
```

### Option 4: Detect planning loop pattern

```python
from difflib import SequenceMatcher

def detect_planning_loop(responses: list[str], similarity_threshold: float = 0.7) -> bool:
    """Detect if recent responses are too similar (revision loop)"""
    if len(responses) < 3:
        return False

    last_three = responses[-3:]
    for i in range(len(last_three) - 1):
        ratio = SequenceMatcher(None, last_three[i], last_three[i+1]).ratio()
        if ratio > similarity_threshold:
            return True  # Two consecutive responses are very similar

    return False

async def run_with_loop_detection(task: str, agent) -> str:
    responses = []

    while True:
        response = await agent.step(task)
        responses.append(response)

        if detect_planning_loop(responses):
            # Force agent out of loop
            force_prompt = (
                "You appear to be revising the same plan repeatedly. "
                "The current plan is good enough. BEGIN EXECUTION NOW. "
                "Do not revise further."
            )
            return await agent.complete(force_prompt)

        if agent.is_done(response):
            return response
```

### Option 5: Separate planner and executor agents

```python
async def plan_then_execute(task: str, planner_agent, executor_agent) -> str:
    """Strict separation: planner creates plan, executor runs it"""

    # Planner creates plan (one shot — no revision)
    plan = await planner_agent.complete(
        f"Create a concise step-by-step plan for: {task}\n"
        f"Output only numbered steps. No explanations. No caveats."
    )
    print(f"Plan created:\n{plan}")

    # Executor runs the plan (no planning allowed)
    result = await executor_agent.complete(
        f"Execute this plan step by step. Do not modify the plan. Just execute:\n{plan}"
    )

    return result

# Planner system prompt: focus only on planning
PLANNER_SYSTEM = "You create plans. Output only the plan. Do not execute anything."

# Executor system prompt: focus only on execution
EXECUTOR_SYSTEM = "You execute plans. Follow each step. Do not plan or revise."
```

## Planning vs. Execution Signals

| Signal | Means | Action |
|---|---|---|
| "I need to consider..." (3rd time) | Planning loop | Force execution |
| "Before I start..." (2nd time) | Over-planning | Force execution |
| "Let me refine the approach..." | Revision loop | Cap revisions |
| "Step 1: ..." | Execution started | Allow to continue |
| "Actually, a better approach..." | Mid-execution replanning | Allow once only |
| "I'll need to account for..." (5th concern) | Analysis paralysis | Force start |

## Expected Token Savings
Infinite planning loop (20 turns): ~30,000 tokens
2-turn plan + execution: ~5,000 tokens

## Environment
- Autonomous agents with complex multi-step tasks
- Source: direct experience, common pattern in agentic frameworks
