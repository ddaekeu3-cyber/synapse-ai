---
layout: solution
title: "Agent Asks Clarifying Questions in Infinite Loop — Never Proceeds"
category: loop-stuck
description: "User asks agent to do a task. Agent asks for clarification. User answers. Agent asks another clarifying question. User answers again. Agent asks a third question. User gives up. Agent never takes action — it keeps asking instead of making a decision."
tags: [loop-stuck, clarification, indecisive, questions, action-bias, decisiveness, system-prompt, agentic]
---

# Agent Asks Clarifying Questions in Infinite Loop — Never Proceeds

## Symptom
- User asks agent to do something; agent asks 5 clarifying questions before taking any action
- After user answers question 1, agent asks question 2, then 3, then 4 — never starts
- Agent asks for the same information that was already in the original request
- Agent defers every decision to the user — "Should I use X or Y?" for trivial choices
- Clarification questions are overly cautious: "Do you want me to write the code?"
- In agentic tasks: agent won't proceed without explicit permission at each micro-step

## Root Cause
The model's default behavior is cautious — it prefers asking over assuming. Without instructions on when to proceed vs. when to ask, the model asks about everything it's uncertain about. This is appropriate for irreversible or high-risk actions, but not for routine tasks. The model also tends to surface all its uncertainty as questions, even when reasonable defaults exist. The fix is to give the model clear guidance on when to act on reasonable assumptions vs. when explicit confirmation is required.

## Fix

### Option 1: System prompt — define when to ask vs. when to proceed

```
System prompt:
"Decision rules — when to ask vs. when to act:

ACT WITHOUT ASKING when:
- You have enough information to begin and the task is reversible
- A reasonable default exists (document format, naming convention, standard approach)
- The user has already answered a similar question earlier in the conversation
- The question is about a trivial implementation detail (e.g., 'Should I use a for loop or list comprehension?')
- You need to make a judgment call — make it and note your assumption

ASK BEFORE PROCEEDING only when:
- An irreversible action is involved (delete, send, publish, deploy)
- The user's intent is genuinely ambiguous in a material way
- Missing information would cause you to do something fundamentally different

NEVER ask more than ONE clarifying question per response.

ALWAYS make a decision about trivial details and move forward.
Example: If format is unspecified, choose a reasonable format and say 'I'll use JSON — let me know if you prefer another format.'

If you are unsure, state your assumption and proceed:
'Assuming you want X — proceeding. Let me know if you need Y instead.'"
```

### Option 2: Clarification budget — limit questions per task

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class ClarificationBudget:
    """
    Limits clarifying questions per task.
    After the budget is exhausted, the agent must proceed with best-effort assumptions.
    """
    max_clarifications: int = 1  # Maximum questions before forced action
    _asked: int = 0
    _assumptions_made: list[str] = field(default_factory=list)

    def can_ask(self) -> bool:
        return self._asked < self.max_clarifications

    def record_question(self):
        self._asked += 1

    def record_assumption(self, assumption: str):
        self._assumptions_made.append(assumption)

    def build_budget_instruction(self) -> str:
        remaining = self.max_clarifications - self._asked
        if remaining > 0:
            return (
                f"You may ask at most {remaining} clarifying question(s) in this response. "
                f"Make reasonable assumptions for everything else and proceed."
            )
        else:
            return (
                "You have used your clarification budget. "
                "Make reasonable assumptions for any remaining unknowns and proceed with the task. "
                "State each assumption clearly before acting on it."
            )

@dataclass
class AgentSession:
    system_base: str
    budget: ClarificationBudget = field(default_factory=lambda: ClarificationBudget(max_clarifications=1))
    history: list[dict] = field(default_factory=list)
    task_started: bool = False

    def add_message(self, role: str, content: str):
        self.history.append({"role": role, "content": content})

    def build_system(self) -> str:
        budget_instruction = self.budget.build_budget_instruction()
        return f"{self.system_base}\n\n{budget_instruction}"

    def send(self, user_message: str, model: str = "claude-sonnet-4-6") -> str:
        self.add_message("user", user_message)

        response = client.messages.create(
            model=model,
            system=self.build_system(),
            messages=self.history,
            max_tokens=2048
        )

        text = response.content[0].text
        self.add_message("assistant", text)

        # Detect if the response is a question or action
        import re
        is_question = bool(re.search(r'\?\s*$', text.strip()))
        if is_question:
            self.budget.record_question()
            print(f"Clarification asked ({self.budget._asked}/{self.budget.max_clarifications})")

        return text

session = AgentSession(
    system_base="You are a helpful coding assistant.",
    budget=ClarificationBudget(max_clarifications=1)  # One question allowed, then proceed
)
```

### Option 3: Detect question loops and force action

```python
import re
from collections import deque

class LoopDetector:
    """
    Detects when the agent is stuck in a clarification loop.
    Forces action after N consecutive question-responses.
    """

    def __init__(self, max_consecutive_questions: int = 2):
        self.max_q = max_consecutive_questions
        self._recent: deque[bool] = deque(maxlen=max_consecutive_questions + 1)

    def record_response(self, response: str) -> bool:
        """
        Record a response. Returns True if a loop is detected.
        """
        is_question = self._is_question(response)
        self._recent.append(is_question)

        if len(self._recent) >= self.max_q and all(self._recent):
            return True  # Loop detected
        return False

    def _is_question(self, text: str) -> bool:
        """Detect if response is primarily a question"""
        # Response ends with a question mark
        ends_with_q = bool(re.search(r'\?\s*$', text.strip()))
        # Response has multiple question marks (multiple questions)
        q_count = text.count('?')
        # Response starts with clarifying phrases
        clarifying_phrases = [
            "before i proceed", "could you clarify", "can you tell me",
            "i need to know", "what would you like", "should i", "do you want"
        ]
        starts_with_clarification = any(
            text.lower().strip().startswith(p) for p in clarifying_phrases
        )
        return ends_with_q or q_count > 1 or starts_with_clarification

loop_detector = LoopDetector(max_consecutive_questions=2)

FORCE_ACTION_INJECTION = """The user is waiting for you to take action.
You have asked enough clarifying questions.
Make reasonable assumptions for any remaining unknowns.
List your assumptions briefly, then PROCEED with the task immediately.
Do NOT ask any more questions."""

async def run_with_loop_detection(
    session: AgentSession,
    user_message: str,
    model: str = "claude-sonnet-4-6"
) -> str:
    response = session.send(user_message, model)
    loop_detected = loop_detector.record_response(response)

    if loop_detected:
        print("LOOP DETECTED: Agent is stuck asking questions. Injecting action directive.")
        # Inject a system-level action forcing directive
        session.history.append({
            "role": "user",
            "content": f"[System: {FORCE_ACTION_INJECTION}]\nPlease proceed with the task now."
        })
        forced_response = session.send("", model)  # Empty message — just trigger action
        return forced_response

    return response
```

### Option 4: Action-bias system prompt patterns

```python
ACTION_BIAS_TEMPLATES = {
    "coding": """
When given a coding task:
1. Start writing code immediately after understanding the core requirement
2. Make standard choices (Python type hints, docstrings, error handling) without asking
3. If a design decision is needed, pick the simpler option and note it in a comment
4. Deliver working code first, offer alternatives after
5. Only stop for: ambiguous business logic, missing API credentials, or security implications
""",

    "writing": """
When given a writing task:
1. Begin drafting immediately — don't ask about tone, format, or length unless critical
2. Default to professional tone, appropriate length, clear structure
3. Write the full draft, then offer variations if needed
4. Ask only if: the target audience is completely unknown, or content requires sensitive expertise
""",

    "research": """
When given a research task:
1. Start with the most reasonable interpretation of the request
2. Use your best judgment on depth, sources, and format
3. State your interpretation at the start: 'I'll focus on X, covering Y and Z aspects'
4. Deliver the research, then ask if a different angle is needed
5. Do not ask before starting — begin immediately
""",

    "agentic": """
Decision framework for multi-step tasks:
- PROCEED automatically: reversible actions, standard operations, following established patterns
- ASK ONCE: irreversible destructive operations (delete, send, publish)
- NEVER ASK: implementation details, naming, formatting, ordering of equivalent options

After the user provides any clarification, proceed through ALL remaining steps without asking again.
State your plan at the start: 'I'll do: 1) X, 2) Y, 3) Z. Proceeding...'
"""
}

def build_action_biased_system(
    base_persona: str,
    domain: str = "agentic"
) -> str:
    template = ACTION_BIAS_TEMPLATES.get(domain, ACTION_BIAS_TEMPLATES["agentic"])
    return f"{base_persona}\n\n{template}"
```

### Option 5: Pre-task agreement — clarify scope upfront in one shot

```python
UPFRONT_SCOPING_PROMPT = """Before beginning this task, if you have questions, ask them ALL in a single message.
Format your questions as a numbered list.
After the user responds, proceed with the full task WITHOUT asking any more questions.
If you have no questions, begin immediately.

Maximum: {max_questions} question(s) allowed."""

def run_with_upfront_scoping(
    task: str,
    max_questions: int = 2,
    model: str = "claude-sonnet-4-6"
) -> str:
    """
    Two-phase approach:
    Phase 1: Model asks all questions at once (if any)
    Phase 2: User answers, model executes fully without further questions
    """
    scoping_system = UPFRONT_SCOPING_PROMPT.format(max_questions=max_questions)

    # Phase 1: scoping
    phase1 = client.messages.create(
        model=model,
        system=scoping_system,
        messages=[{"role": "user", "content": task}],
        max_tokens=512
    )
    phase1_text = phase1.content[0].text

    # Check if any questions were asked
    has_questions = '?' in phase1_text and not phase1_text.strip().startswith(
        ('I ', 'Here ', 'Let ', 'Sure', 'Based', 'The ', 'To ')
    )

    if not has_questions:
        # No questions — this IS the result
        return phase1_text

    # Phase 2: get user's answers
    print(f"Agent questions:\n{phase1_text}")
    user_answers = input("Your answers: ")

    # Phase 3: execute with no further questions allowed
    phase2_system = (
        "The user has answered all your questions. "
        "Proceed with the task completely and without asking any further questions."
    )

    phase2 = client.messages.create(
        model=model,
        system=phase2_system,
        messages=[
            {"role": "user", "content": task},
            {"role": "assistant", "content": phase1_text},
            {"role": "user", "content": user_answers}
        ],
        max_tokens=4096
    )

    return phase2.content[0].text
```

### Option 6: Track question-to-action ratio as a quality metric

```python
from dataclasses import dataclass, field
import re

@dataclass
class AgentQualityMetrics:
    """Track agent behavior quality — flag over-questioning"""
    _responses: list[dict] = field(default_factory=list)

    def record_response(self, response: str, led_to_action: bool):
        questions = len(re.findall(r'\?', response))
        self._responses.append({
            "response_length": len(response.split()),
            "question_count": questions,
            "led_to_action": led_to_action,
            "is_pure_question": questions > 0 and led_to_action == False
        })

    @property
    def question_rate(self) -> float:
        if not self._responses:
            return 0
        pure_questions = sum(1 for r in self._responses if r["is_pure_question"])
        return pure_questions / len(self._responses)

    @property
    def avg_questions_per_clarification(self) -> float:
        clarifications = [r for r in self._responses if r["is_pure_question"]]
        if not clarifications:
            return 0
        return sum(r["question_count"] for r in clarifications) / len(clarifications)

    def report(self) -> dict:
        return {
            "total_responses": len(self._responses),
            "question_only_responses": sum(1 for r in self._responses if r["is_pure_question"]),
            "action_responses": sum(1 for r in self._responses if r["led_to_action"]),
            "question_rate": f"{self.question_rate*100:.0f}%",
            "status": (
                "OVER-QUESTIONING" if self.question_rate > 0.3 else
                "OK" if self.question_rate > 0.1 else
                "DECISIVE"
            )
        }

metrics = AgentQualityMetrics()

# Target metrics:
# Question rate < 10%: agent is appropriately decisive
# Question rate 10-30%: acceptable for complex domains
# Question rate > 30%: over-questioning — adjust system prompt
```

## When to Ask vs. When to Act

| Situation | Action | Rationale |
|---|---|---|
| Reversible task, reasonable defaults exist | Act, state assumptions | Low risk of wrong assumption |
| Implementation detail choice | Act, pick simpler option | User doesn't care about this |
| Irreversible destructive action | Ask once | Cannot undo |
| Sending to external parties | Ask once | Real-world side effect |
| Genuinely ambiguous core intent | Ask once | Material impact on result |
| Same info asked in original request | Act | Answer is already there |
| User already answered this type of question | Act | Infer from prior answer |

## Expected Token Savings
User abandons after 4 clarifying questions → starts new session: ~6,000 tokens wasted
Decisive agent proceeds with stated assumptions: task completed in 1-2 exchanges

## Environment
- Any conversational or agentic agent; critical for task automation agents, coding assistants, and any agent expected to complete multi-step tasks autonomously
- Source: direct experience; over-questioning is the most common behavior complaint from power users of AI agents — they want action, not interrogation
