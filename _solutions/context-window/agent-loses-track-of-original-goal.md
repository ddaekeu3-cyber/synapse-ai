---
layout: solution
title: "Agent Loses Track of Original Goal Over Long Conversation — Goal Drift"
category: context-window
description: "Agent starts with a clear task but after many tool calls and sub-tasks, forgets the original objective. Completes sub-tasks correctly but never finishes the main goal."
tags: [goal-drift, context, long-session, objective, planning]
---

# Agent Loses Track of Original Goal Over Long Conversation — Goal Drift

## Symptom
- Agent was asked to "build a REST API" — after 20 turns, it's only working on database schemas
- Original task was "fix the login bug" — agent is now refactoring unrelated auth code
- Agent completes many steps but never reports the main task as done
- Asking "are we done?" gets a confused response listing sub-tasks
- Later responses contradict earlier decisions made in the same session

## Root Cause
As conversations grow, the original goal is pushed deeper into the context window. The model's attention is weighted toward recent tokens. Without explicit reminders, the original objective loses influence over the model's behavior relative to the immediate sub-task currently in focus.

## Fix

### Option 1: Restate the goal in every system message turn

```python
ORIGINAL_GOAL = "Build a REST API for user authentication with JWT tokens"

def build_messages_with_goal(history: list, new_user_message: str) -> list:
    """Inject goal reminder into every API call"""
    system = f"""You are a software engineering assistant.

PRIMARY GOAL (always keep this in mind): {ORIGINAL_GOAL}

Current task: Complete the primary goal. When working on sub-tasks, always
remember how they contribute to the primary goal. When the primary goal is
fully achieved, explicitly state: "PRIMARY GOAL COMPLETE."
"""
    return [
        {"role": "system", "content": system},
        *history,
        {"role": "user", "content": new_user_message}
    ]
```

### Option 2: Goal anchor message pinned at start

```python
def create_goal_anchored_session(goal: str) -> list:
    """Create a session with the goal anchored as first exchange"""
    return [
        {
            "role": "user",
            "content": f"[GOAL] {goal}\n\nConfirm you understand this goal and will keep it in focus throughout our entire session."
        },
        {
            "role": "assistant",
            "content": f"Confirmed. My primary goal is: {goal}\n\nI will maintain focus on this throughout our session and only consider a task complete when this goal is fully achieved."
        }
    ]

history = create_goal_anchored_session("Fix the authentication bug causing 401 errors on mobile")
# Continue adding messages to history — goal is always visible at the start
```

### Option 3: Structured task tracker in context

```python
TASK_STATE_TEMPLATE = """
=== TASK STATE (update this as you work) ===
PRIMARY GOAL: {goal}
STATUS: {status}  # in-progress / blocked / complete
COMPLETED STEPS:
{completed}
CURRENT STEP: {current}
REMAINING STEPS:
{remaining}
================================================
"""

async def agent_with_task_tracking(goal: str):
    task_state = {
        "goal": goal,
        "status": "in-progress",
        "completed": [],
        "current": "Planning",
        "remaining": ["To be determined after planning"]
    }

    while task_state["status"] != "complete":
        state_summary = TASK_STATE_TEMPLATE.format(
            goal=task_state["goal"],
            status=task_state["status"],
            completed="\n".join(f"  ✓ {s}" for s in task_state["completed"]),
            current=task_state["current"],
            remaining="\n".join(f"  - {s}" for s in task_state["remaining"])
        )

        response = await agent.complete(
            system=f"You are working on a task. Current state:\n{state_summary}",
            message="What is the next action toward the primary goal?"
        )

        # Parse response to update task state
        task_state = parse_task_update(response, task_state)
```

### Option 4: Periodic goal check-in

```python
GOAL_CHECKIN_INTERVAL = 5  # Every 5 turns

async def managed_session_with_checkin(goal: str):
    history = []
    turn = 0

    while True:
        user_input = await get_user_input()
        history.append({"role": "user", "content": user_input})

        # Periodic goal check-in
        if turn > 0 and turn % GOAL_CHECKIN_INTERVAL == 0:
            checkin = await agent.complete(
                history + [{
                    "role": "user",
                    "content": f"GOAL CHECK: Our original goal was: '{goal}'. "
                               f"In one sentence, how does your last response advance this goal? "
                               f"If we've completed it, say GOAL COMPLETE."
                }]
            )
            print(f"[Goal check turn {turn}]: {checkin}")
            if "GOAL COMPLETE" in checkin:
                print("Primary goal achieved!")
                return

        response = await agent.complete(history)
        history.append({"role": "assistant", "content": response})
        turn += 1
```

### Option 5: Hierarchical goals — main goal always visible

```
System prompt:
"Task hierarchy (always maintain this structure):

LEVEL 1 — PRIMARY GOAL (never lose sight of this):
  Build a user authentication system with login, logout, JWT refresh

LEVEL 2 — CURRENT PHASE:
  [Update this as you progress: Planning / Implementation / Testing / Done]

LEVEL 3 — IMMEDIATE STEP:
  [What you are doing right now]

Rules:
- Before each response, silently verify: does this action advance Level 1?
- If spending more than 3 turns on a Level 3 step, re-evaluate if it's necessary
- When Level 1 is complete, say 'PRIMARY GOAL ACHIEVED' and stop"
```

## Goal Drift Warning Signs

| Sign | What it means |
|---|---|
| Agent asks about scope of unrelated feature | Drifted from original task |
| Response doesn't mention original goal | Goal dropped from attention |
| "While I'm at it..." refactoring | Scope creep / drift |
| Sub-task takes more than 5 turns | May have become the new de-facto goal |
| Agent marks sub-task done but doesn't continue | Forgot there's a larger goal |

## Expected Token Savings
Goal drift + recovery: ~20,000 tokens (re-explaining, backtracking)
Goal anchor in system prompt: ~200 tokens upfront

## Environment
- Long-running agent sessions with multi-step tasks
- Source: direct experience with complex agent workflows
