---
layout: solution
title: "Agent Doesn't Stop After Completing the Task — Keeps Adding Unnecessary Steps"
category: general
description: "Agent completes the requested task but continues adding unrequested steps, additional analysis, edge case handling, or cleanup. Task scope creeps until token budget or context is exhausted."
tags: [scope-creep, task-completion, stopping, over-engineering]
---

# Agent Doesn't Stop After Completing the Task — Keeps Adding Unnecessary Steps

## Symptom
- Agent completes the task (e.g., writes the function) but then:
  - Adds unrequested tests
  - Refactors surrounding code that wasn't mentioned
  - Adds error handling for edge cases that don't apply
  - Writes documentation nobody asked for
  - "While I'm here, I also noticed..."
- Each additional step may introduce bugs or unwanted changes
- Simple tasks take 10x more tokens than needed
- User has to manually undo the unrequested additions

## Root Cause
The model is trained to be helpful, and "helpful" in its training data often means thorough. Without an explicit stopping condition, the agent continues until it runs out of context or reaches an internal confidence threshold. The helpfulness heuristic overrides scope discipline.

## Fix

### Option 1: Explicit stopping instructions in system prompt

```
System prompt:
"Complete ONLY what is explicitly requested. After completing the task:
1. State clearly what you did
2. STOP

Do not add tests, documentation, refactoring, error handling, or cleanup
unless explicitly asked. If you notice additional improvements, mention them
briefly as suggestions — do not implement them unasked."
```

### Option 2: Define completion criteria in the task

```
# WEAK task definition
"Fix the bug in process_order()"

# STRONG task definition with explicit scope
"Fix ONLY the null check bug in process_order() on line 47.
Do not change any other code. Done = the null check is fixed, nothing else changed."
```

### Option 3: Single-action task decomposition

Break multi-step work into explicit single actions:

```python
tasks = [
    "Step 1 only: Add the null check to process_order(). Stop after this.",
    "Step 2 only: Add one test for the null check. Stop after this.",
    "Step 3 only: Update the docstring. Stop after this.",
]
# Run each task separately, review before proceeding
```

### Option 4: Scope boundary in system prompt

```
System prompt:
"Your scope is limited to files and functions explicitly mentioned in each request.
Never modify files not mentioned. Never add new files unless asked.
If implementing a feature requires touching an unmentioned file, ask first."
```

### Option 5: Output review gate

```python
async def task_with_scope_check(task, agent):
    response = await agent.complete(task)
    changes = get_changed_files(response)

    # Verify changes are within expected scope
    expected_files = extract_mentioned_files(task)
    unexpected = [f for f in changes if f not in expected_files]

    if unexpected:
        print(f"Warning: Agent modified unexpected files: {unexpected}")
        approval = input("Accept these changes? (y/n): ")
        if approval.lower() != 'y':
            revert_changes(unexpected)

    return response
```

## Expected Token Savings
Agent with scope creep on simple task: 5,000–20,000 extra tokens
This fix: saves majority of unnecessary work tokens

## Environment
- Any code-editing or task-executing agent
- Higher risk with: open-ended task descriptions, large codebases
- Source: direct experience, extremely common agent behavior pattern
