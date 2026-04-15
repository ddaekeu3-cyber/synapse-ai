---
layout: solution
title: "Agent Keeps Asking the Same Question Different Ways — Reformulation Loop"
category: loop-stuck
description: "Agent asks for the same information repeatedly, rephrasing each time. 'What's the target URL?' then 'Which endpoint should I call?' then 'Where is the API hosted?' — all the same question."
tags: [loop, reformulation, clarification, stuck, repetitive-questions]
---

# Agent Keeps Asking the Same Question Different Ways — Reformulation Loop

## Symptom
- Agent asks "What's the database name?" → user answers → agent asks "Which schema should I use?"
- Agent rephrases the same underlying question 3-4 times
- User has already answered but agent doesn't recognize the answer as sufficient
- Agent seems to forget answers given 2-3 turns ago
- Loop only breaks when user gives up and provides exhaustive detail upfront

## Root Cause
The agent has an information gap it cannot resolve by inference. Without clear stopping criteria for clarification, it generates new phrasings of the same question hoping a different formulation gets a more specific answer. Often the model doesn't map the user's earlier answer to its internal question correctly.

## Fix

### Option 1: Require the agent to acknowledge answers before asking next question

```
System prompt:
"Clarification protocol:
1. Ask ONE clarifying question at a time
2. Before asking a new question, explicitly state what you learned from the previous answer:
   'Understood: the database is PostgreSQL on localhost:5432.'
3. Only ask a follow-up if the acknowledged answer genuinely doesn't cover what you need
4. If you catch yourself rephrasing a question you already asked, STOP — use your best inference instead"
```

### Option 2: Enumerate all needed information upfront, then ask once

```
System prompt:
"Before starting any task, identify ALL information you need.
List the unknowns, then ask them in a single numbered list.
Do NOT ask questions one at a time across multiple turns.

Example:
'Before I proceed, I need:
1. Target database host (e.g., localhost:5432)
2. Schema name
3. Authentication method (password/IAM/cert)
Please answer all three and I'll start immediately.'"
```

### Option 3: Map answers to questions explicitly in code

```python
class ClarificationTracker:
    def __init__(self):
        self.questions_asked = {}  # question_key -> question_text
        self.answers_received = {}  # question_key -> answer_text

    def ask(self, key: str, question: str) -> str | None:
        """Returns None if already answered, otherwise the question"""
        if key in self.answers_received:
            return None  # Already know this
        if key in self.questions_asked:
            return None  # Already asked, wait for answer
        self.questions_asked[key] = question
        return question

    def receive_answer(self, key: str, answer: str):
        self.answers_received[key] = answer

    def get(self, key: str) -> str | None:
        return self.answers_received.get(key)

tracker = ClarificationTracker()

# In agent loop:
if not tracker.get("db_host"):
    q = tracker.ask("db_host", "What is the database host?")
    if q:
        return q  # Ask only once
```

### Option 4: Threshold — infer after N failed clarifications

```python
MAX_CLARIFICATIONS = 2

async def run_with_clarification_limit(task, agent):
    clarification_count = 0
    context = task.initial_context

    while True:
        result = await agent.step(context)

        if result.type == "clarification_needed":
            clarification_count += 1
            if clarification_count > MAX_CLARIFICATIONS:
                # Stop asking, use best inference
                context += f"\n\nNote: Maximum clarifications reached. Proceed with best assumptions. State your assumptions explicitly in the response."
                result = await agent.step(context)
                return result
            else:
                user_answer = await get_user_input(result.question)
                context += f"\nQ: {result.question}\nA: {user_answer}"
        else:
            return result
```

### Option 5: Detect reformulation loops at runtime

```python
from difflib import SequenceMatcher

def is_similar_question(q1: str, q2: str, threshold=0.6) -> bool:
    """Detect if two questions are semantically similar"""
    ratio = SequenceMatcher(None, q1.lower(), q2.lower()).ratio()
    return ratio > threshold

class ReformulationDetector:
    def __init__(self):
        self.questions_history = []

    def check_and_record(self, new_question: str) -> bool:
        """Returns True if this looks like a reformulation"""
        for past_q in self.questions_history:
            if is_similar_question(new_question, past_q):
                return True  # Reformulation detected
        self.questions_history.append(new_question)
        return False

detector = ReformulationDetector()

# In agent loop before sending question:
if detector.check_and_record(agent_question):
    # Inject instruction to stop reformulating
    inject_message = "You already asked a similar question. Use your best inference based on what you already know and proceed."
```

## Anti-Patterns to Avoid

| Anti-pattern | Better approach |
|---|---|
| Ask "What URL?" then "Which endpoint?" | Ask "What is the full API endpoint URL?" |
| Ask one question, get answer, ask same thing rephrased | Acknowledge the answer explicitly, then move on |
| Ask for more detail when inference would work | State assumption, proceed, offer to adjust |
| Unlimited clarification turns | Cap at 2-3, then infer with stated assumptions |

## Expected Token Savings
10-turn reformulation loop: ~8,000 tokens
Upfront multi-question ask + proceed: ~1,000 tokens

## Environment
- Any conversational agent with clarification behavior
- Source: direct experience, common failure mode in task-oriented agents
