---
layout: solution
title: "Agent Gets Stuck in a Clarification Loop"
category: loop-stuck
description: "Agent keeps asking for more information on every turn instead of making reasonable assumptions and proceeding with the task."
tags: [loop-stuck, prompt-engineering, user-experience, general, reliability]
---

## Symptom

The agent responds to every user request with a question. "What format would you like?" "Can you clarify what you mean by X?" "Would you prefer A or B?" The user answers, the agent asks another question, and the task never starts. In agentic pipelines with no human in the loop, the agent stalls indefinitely waiting for input that will never arrive.

## Root Cause

Without explicit instructions to proceed under uncertainty, the model's default behaviour is to resolve ambiguity by asking rather than assuming. This is individually reasonable but catastrophic in aggregate — a model that asks even one question per turn on a 10-step task requires 20 turns instead of 10. In automated pipelines, a single unanswered question halts the entire workflow.

## Fix

### Option 1 — System prompt: assume and proceed, note assumptions at the end

```python
import anthropic

client = anthropic.Anthropic()

SYSTEM = """You are a productive assistant that completes tasks efficiently.

Rules:
- Make reasonable default assumptions when details are missing.
- NEVER ask clarifying questions before starting the task.
- Complete the task first, then list your assumptions at the end under "Assumptions made:".
- If a decision is genuinely blocked (e.g., you need a password you cannot guess), state what is missing and stop — but only for truly blocking information, not preferences."""

def ask(prompt: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text

# Vague requests that used to trigger clarification loops
print(ask("Write me a function to process data."))
print("---")
print(ask("Send an email about the meeting."))
print("---")
print(ask("Create a database schema for a blog."))
```

**Expected Token Savings:** Eliminates 1–5 clarification turns per task; each skipped turn saves 200–600 tokens.
**Environment:** Interactive assistants and automated pipelines alike; apply this system prompt universally.

---

### Option 2 — Assumption manifest: pre-fill defaults before calling the model

```python
import anthropic

client = anthropic.Anthropic()

DEFAULT_ASSUMPTIONS = {
    "language":        "Python 3.11",
    "code_style":      "PEP 8, type annotations",
    "error_handling":  "raise exceptions, no silent failures",
    "output_format":   "markdown with code blocks",
    "test_framework":  "pytest",
    "database":        "PostgreSQL via SQLAlchemy",
    "auth":            "JWT bearer tokens",
}

def build_system(task_domain: str = "software engineering") -> str:
    defaults_str = "\n".join(f"- {k}: {v}" for k, v in DEFAULT_ASSUMPTIONS.items())
    return f"""You are a {task_domain} assistant. Apply these defaults unless the user specifies otherwise:

{defaults_str}

Do not ask for clarification on any of the above. Proceed immediately with the task.
State which defaults you used only if non-obvious."""

def ask(prompt: str, domain: str = "software engineering") -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=build_system(domain),
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text

print(ask("Write a login endpoint."))
print("---")
print(ask("Add unit tests for the user service."))
```

**Expected Token Savings:** Default manifest eliminates the most common class of clarification questions (language, style, framework); each question skipped saves a full round-trip.
**Environment:** Domain-specific coding assistants; maintain the DEFAULT_ASSUMPTIONS dict as a project config.

---

### Option 3 — Clarification budget: allow at most one question per task

```python
import anthropic

client = anthropic.Anthropic()

SYSTEM = """You are a task-execution assistant.

Clarification policy:
- You may ask AT MOST ONE clarifying question per task, and only if the missing information is absolutely necessary to produce any useful output.
- If you can produce a reasonable result without the information, do so.
- If you choose to ask a question, ask all your questions in a single message — not one at a time.
- Never ask follow-up questions after the user responds. Use their answer and proceed."""

def chat(history: list[dict], user_message: str) -> tuple[str, list[dict]]:
    history = history + [{"role": "user", "content": user_message}]
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=SYSTEM,
        messages=history,
    )
    reply = response.content[0].text
    history = history + [{"role": "assistant", "content": reply}]
    return reply, history

history = []

# Simulate a conversation
reply, history = chat(history, "Build me a REST API.")
print(f"Agent: {reply[:200]}\n")

reply, history = chat(history, "Make it handle users and products.")
print(f"Agent: {reply[:200]}\n")

# Count question marks to check if clarification loop has started
q_count = sum(1 for m in history if m["role"] == "assistant" and "?" in m["content"])
print(f"Questions asked: {q_count} (budget: 1)")
```

**Expected Token Savings:** Hard limit prevents compounding clarification loops; one question maximum vs. potentially unbounded.
**Environment:** Interactive agents where some clarification is acceptable but must be bounded.

---

### Option 4 — Detect and break clarification loops in the agentic control loop

```python
import anthropic
import re

client = anthropic.Anthropic()

QUESTION_PATTERN = re.compile(r"\?")
MAX_CONSECUTIVE_QUESTIONS = 2

def count_questions(text: str) -> int:
    return len(QUESTION_PATTERN.findall(text))

def is_clarification_turn(reply: str) -> bool:
    """Heuristic: reply ends with a question and contains few action words."""
    stripped = reply.strip()
    ends_with_question = stripped.endswith("?")
    action_words = {"here", "created", "updated", "added", "done", "complete", "result", "output"}
    has_action = any(w in stripped.lower() for w in action_words)
    return ends_with_question and not has_action and count_questions(reply) >= 1

def run_agentic_loop(task: str, max_steps: int = 8) -> str:
    messages = [{"role": "user", "content": task}]
    consecutive_questions = 0

    for step in range(max_steps):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system="Complete tasks by acting, not by asking questions. If uncertain, make a reasonable assumption.",
            messages=messages,
        )
        reply = response.content[0].text
        messages.append({"role": "assistant", "content": reply})

        if is_clarification_turn(reply):
            consecutive_questions += 1
            print(f"[loop] clarification detected (streak={consecutive_questions})")

            if consecutive_questions >= MAX_CONSECUTIVE_QUESTIONS:
                # Break the loop by providing a generic "proceed" instruction
                print("[loop] breaking clarification loop — injecting proceed directive")
                messages.append({
                    "role": "user",
                    "content": "Make reasonable assumptions and proceed with the task. Do not ask further questions.",
                })
                consecutive_questions = 0
                continue
        else:
            consecutive_questions = 0

        # Check if task is done
        done_signals = ["done", "complete", "finished", "here is", "here's", "result:"]
        if any(sig in reply.lower() for sig in done_signals):
            return reply

    return messages[-1]["content"]

result = run_agentic_loop("Write a Python script to parse CSV files.")
print(result[:400])
```

**Expected Token Savings:** Loop detection injects a single "proceed" message instead of waiting for human input; prevents unbounded stalls in automated pipelines.
**Environment:** Autonomous agentic pipelines with no human in the loop; essential for unattended batch agents.

---

### Option 5 — Confidence-gated proceeding: act if confidence > threshold

```python
import json
import anthropic

client = anthropic.Anthropic()

CONFIDENCE_SYSTEM = """Before responding to a task, assess your confidence (0-100) that you have enough information to produce a useful result.

Return a JSON object:
{
  "confidence": <0-100>,
  "blocking_gaps": ["list only truly blocking unknowns"],
  "safe_assumptions": ["assumptions you can make to proceed"],
  "can_proceed": <true if confidence >= 60>
}

Then immediately below the JSON, write your response to the task (even at lower confidence, provide your best attempt)."""

def smart_task(prompt: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=768,
        system=CONFIDENCE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text

    # Parse the JSON header
    try:
        json_end = text.index("\n}", 0, 400) + 2
        meta      = json.loads(text[:json_end])
        task_out  = text[json_end:].strip()

        if meta.get("blocking_gaps"):
            print(f"[confidence={meta['confidence']}] blocking gaps: {meta['blocking_gaps']}")
        if meta.get("safe_assumptions"):
            print(f"[confidence={meta['confidence']}] assuming: {meta['safe_assumptions']}")
        return task_out
    except (ValueError, json.JSONDecodeError):
        return text

tasks = [
    "Generate a weekly report.",
    "Fix the bug in my code.",
    "Create a user registration form in React with email, password, and confirm-password fields.",
]
for t in tasks:
    print(f"\nTask: {t!r}")
    print(smart_task(t)[:300])
```

**Expected Token Savings:** Explicit confidence scoring surfaces only truly blocking gaps; agent proceeds at 60+ confidence instead of asking about every uncertainty.
**Environment:** Tasks with variable information completeness; makes the proceed/ask decision transparent and auditable.

---

### Option 6 — Two-pass strategy: draft first, refine if needed

```python
import anthropic

client = anthropic.Anthropic()

DRAFT_SYSTEM = """You are a fast-action assistant. When given a task:
1. Immediately produce a complete draft based on the most reasonable interpretation.
2. At the end, add a one-line note: "Note: assumed X; reply to adjust."
Never ask questions before drafting."""

REFINE_SYSTEM = """You are a refinement assistant. You will receive a draft and user feedback.
Apply the feedback precisely and return the updated version."""

def two_pass(task: str, feedback: str | None = None) -> str:
    if feedback is None:
        # First pass: draft immediately
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=768,
            system=DRAFT_SYSTEM,
            messages=[{"role": "user", "content": task}],
        )
        return response.content[0].text
    else:
        # Second pass: apply specific feedback
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=768,
            system=REFINE_SYSTEM,
            messages=[
                {"role": "user",      "content": f"Task: {task}"},
                {"role": "assistant", "content": two_pass(task)},   # re-draft for context
                {"role": "user",      "content": f"Feedback: {feedback}"},
            ],
        )
        return response.content[0].text

# Vague task — draft immediately
draft = two_pass("Write a function to process user data.")
print("=== DRAFT ===")
print(draft[:400])

# User provides specific feedback — refine without asking more questions
refined = two_pass("Write a function to process user data.", "Make it async and add type hints.")
print("\n=== REFINED ===")
print(refined[:400])
```

**Expected Token Savings:** Draft-first eliminates the clarification round-trip entirely; feedback is specific and actionable, not a back-and-forth.
**Environment:** User-facing assistants where producing something concrete is always better than asking; show draft, invite feedback.

---

## Comparison

| Option | Mechanism | Human Required? | Works in Automation? | Best For |
|---|---|---|---|---|
| 1. System prompt rules | Prompt instruction | No | Yes | Universal starting point |
| 2. Assumption manifest | Pre-filled defaults | No | Yes | Domain-specific agents |
| 3. Question budget | 1-question limit | Partial | Partial | Interactive with guard rails |
| 4. Loop detection | Control-loop monitor | No | Yes | Autonomous agentic pipelines |
| 5. Confidence gate | Self-assessment JSON | No | Yes | Variable-completeness tasks |
| 6. Draft-first | Two-pass refinement | Partial | Partial | User-facing assistants |
