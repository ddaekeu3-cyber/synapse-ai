---
layout: solution
title: "Agent Plans Endlessly Without Executing — Stuck in Analysis Paralysis"
category: loop-stuck
description: "Agent generates detailed plans, outlines, and analysis but never takes action. Every response produces more planning. The agent never actually writes code, runs commands, or produces output."
tags: [planning, analysis-paralysis, execution, stuck, autonomous]
---

# Agent Plans Endlessly Without Executing — Stuck in Analysis Paralysis

## Symptom
- Agent produces detailed plans with many bullet points — then asks for confirmation
- Next prompt: another plan with more detail — still no action
- Agent says "I'll first need to understand X, then analyze Y, then plan Z..."
- After 5 turns, nothing concrete has been produced
- Agent generates sophisticated-sounding reasoning but no deliverables
- Asking "just do it" results in more planning

## Root Cause
The agent is over-optimizing for thoroughness and correctness before acting. Without an explicit execution bias, the model may treat planning as the task rather than as a precursor to the task. Confirmation-seeking loops compound this.

## Fix

### Option 1: "Execute, don't plan" instruction

```
System prompt:
"You are an execution agent. Your job is to DO, not to plan.

Rules:
- Never produce a numbered plan without executing the first step in the same response
- For tasks you can complete in <10 tool calls: start immediately
- Planning is only allowed for tasks taking >1 hour of work
- When in doubt: start and adjust, don't plan and ask

Wrong: 'Here's my plan: 1) First I'll... 2) Then I'll...'
Right: [starts doing the task] 'Done. Next step: [does it]'"
```

### Option 2: Time-box planning to 1 response

```
System prompt:
"You may plan for at most ONE response. After that, every response must contain
at least one concrete action (file edit, command run, code written, API call made).
If you catch yourself planning without acting, stop and execute the first step."
```

### Option 3: Task decomposition with immediate start

```
System prompt:
"Task execution format:
1. ONE sentence describing what you're doing
2. Do it immediately (write the code, run the command, etc.)
3. Report what you did (1-2 sentences)
4. State the next step

Never: multi-paragraph preamble, numbered analysis lists, or 'I would need to...'
Always: act first, explain briefly after"
```

### Option 4: Detect planning loops and break them

```python
def detect_planning_loop(agent_messages, last_n=4):
    """Check if recent responses are all planning without output"""
    recent = agent_messages[-last_n:]

    planning_signals = [
        "first, i'll", "step 1:", "my plan is", "approach:", "i would need to",
        "before i can", "i'll need to understand", "i should analyze"
    ]

    all_planning = all(
        any(s in msg['content'].lower() for s in planning_signals)
        and not has_code_block(msg['content'])
        and not has_tool_call(msg)
        for msg in recent
    )

    if all_planning:
        return "Agent appears stuck in planning loop. Injecting execution prompt."
    return None

def inject_execution_prompt(history):
    return history + [{
        "role": "user",
        "content": "Stop planning. Execute the first step RIGHT NOW. "
                   "Write code or run a command in this response."
    }]
```

### Option 5: Reward system in prompt

```
System prompt:
"Progress is measured by:
✓ Lines of code written
✓ Commands run
✓ Files created or edited
✓ Tests passing

NOT measured by:
✗ Quality of plans
✗ Thoroughness of analysis
✗ Number of considerations listed

Start producing measurable output immediately."
```

## Expected Token Savings
5 planning-only exchanges before any output: ~8,000 tokens overhead
Execution-biased agent: delivers output in response 1

## Environment
- Task-executing agents, especially with complex or open-ended requests
- Source: direct experience, common in models trained for caution
