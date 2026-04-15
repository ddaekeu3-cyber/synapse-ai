---
layout: solution
title: "Agent Asks Too Many Clarifying Questions Before Starting — Over-Clarification"
category: general
description: "Agent asks 5–10 clarifying questions before taking any action. Simple tasks get blocked by excessive confirmation-seeking. The agent never starts without full specification."
tags: [over-clarification, questions, autonomous, task-execution]
---

# Agent Asks Too Many Clarifying Questions Before Starting — Over-Clarification

## Symptom
- User: "Write a function to parse JSON"
- Agent: "Before I start, could you tell me: 1) What programming language? 2) What version? 3) Will you handle nested objects? 4) Do you want error handling? 5) Should I write tests? 6) What's the function signature? 7) ..."
- Simple requests blocked by multi-question interrogation
- Agent won't start until every detail is specified
- User has to answer 7 questions for a 5-line task
- Autonomous tasks never run — always waiting for human input

## Root Cause
The agent is trained to be thorough and avoid mistakes, leading to over-specification-seeking behavior. Without explicit instruction about when to proceed with reasonable defaults vs. when to ask, the agent defaults to maximum clarification.

## Fix

### Option 1: "Bias to action" system prompt instruction

```
System prompt:
"Bias strongly toward action over clarification.

For any request:
- If you can make a reasonable assumption, make it and proceed
- State your assumptions at the start of your response, don't ask about them
- Only ask a question if the answer would FUNDAMENTALLY change your approach
- Never ask more than ONE question before starting
- For small tasks (<30 minutes): just do it

Bad: 'Should I use TypeScript or JavaScript?'
Good: 'I'll implement this in TypeScript (your project uses TypeScript). Here's the function: [code]'

If you made a wrong assumption, it's faster to correct than to ask upfront."
```

### Option 2: Limit questions explicitly

```
System prompt:
"You may ask at most ONE clarifying question before starting work.
If you need to choose between multiple unknowns, pick the most important one.
For everything else, proceed with the most reasonable default."
```

### Option 3: Assumptions-first pattern

Instruct the agent to state assumptions, not ask about them:

```
System prompt:
"Format for starting any task:
1. State your assumptions (do not ask — just state what you'll do)
2. Start the work immediately
3. At the end: 'Let me know if any assumption was wrong and I'll adjust'

Example:
'Assumptions: Python 3.10+, FastAPI framework, returns JSON. If different, let me know.
[starts writing code immediately]'"
```

### Option 4: Define project-level defaults

Provide context that answers common questions preemptively:

```
System prompt:
"Project context (use these defaults, don't ask):
- Language: Python 3.11
- Framework: FastAPI
- Testing: pytest
- Style: Google style guide, type hints required
- Error handling: raise exceptions, don't return None on error
- Format: single file unless explicitly asked to split"
```

### Option 5: Treat questions as suggestions

```
System prompt:
"Instead of asking questions, present one approach and start implementing.
Format: 'I'll do X [with brief reason]. Here's the implementation: [code]
Alternative approach if needed: Y'

This gives the user what they asked for immediately, plus the option to redirect."
```

## Example Transformation

```
# BEFORE (over-clarification)
User: "Add error handling to this function"
Agent: "Before I proceed, I need to know:
1. What type of error handling? Try-catch or error codes?
2. Should I log errors? To stdout or a file?
3. What should happen on error — retry or fail?
4. Do you want custom exception classes?
5. Should I add tests for the error cases?"

# AFTER (bias to action)
User: "Add error handling to this function"
Agent: "I'll add try-except with logging to stdout and re-raise after logging
(standard for this codebase). Here's the updated function: [code]
If you prefer silent failures or custom exceptions, let me know."
```

## Expected Token Savings
5 back-and-forth clarification rounds: ~3,000 tokens overhead
Bias-to-action approach: ~200 tokens (assumption statement) + immediate work

## Environment
- Any task-executing agent, especially autonomous or semi-autonomous ones
- Source: direct experience, common LLM behavior pattern
