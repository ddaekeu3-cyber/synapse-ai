---
layout: solution
title: "Agent Loops on Ambiguous User Intent"
category: loop-stuck
description: "When the user's request is unclear, the agent asks one clarifying question, gets a partial answer, asks another slightly different question, and repeats indefinitely without making progress."
tags: [loop, clarification, intent, disambiguation, ambiguity, tool-use, structured-output]
---

## Symptom

User: "Process my files."
Agent: "Which files?"
User: "The ones from yesterday."
Agent: "Which format do you need?"
User: "The usual one."
Agent: "Do you mean CSV or JSON?"
User: "Just do what we always do."
Agent: "I need to know the output directory..."

The agent circles through partial clarifications indefinitely, never accumulating enough information to act. Each turn resolves one ambiguity but reveals two more. The user grows frustrated and abandons the session.

## Root Cause

The agent asks for information one field at a time, with no model of what it still needs before it can act. It has no concept of "minimum viable specification" — the exact set of parameters required to execute the task. Without knowing the gap between what it has and what it needs, it asks redundant or out-of-order questions and loops.

## Fix

---

### Option 1: Structured Intent Extraction with Gap Analysis

Extract all required fields in one pass. Only ask for missing fields — in a single consolidated question.

```python
import json
import anthropic
from pydantic import BaseModel
from typing import Optional

client = anthropic.Anthropic()


class FileProcessingIntent(BaseModel):
    source_directory: Optional[str] = None
    file_pattern: Optional[str] = None      # e.g., "*.csv", "report_*.xlsx"
    output_format: Optional[str] = None     # csv | json | parquet
    output_directory: Optional[str] = None
    date_filter: Optional[str] = None       # e.g., "yesterday", "2025-04-14"

    def missing_fields(self) -> list[str]:
        return [f for f, v in self.model_dump().items() if v is None]

    def is_complete(self) -> bool:
        # Only source_directory and output_format are truly required
        return self.source_directory is not None and self.output_format is not None


EXTRACT_TOOL = {
    "name": "extract_intent",
    "description": "Extract file processing parameters from user message.",
    "input_schema": {
        "type": "object",
        "properties": {
            "source_directory": {"type": "string", "description": "Source directory path, if mentioned"},
            "file_pattern":     {"type": "string", "description": "File pattern or extension, if mentioned"},
            "output_format":    {"type": "string", "description": "Output format (csv/json/parquet), if mentioned"},
            "output_directory": {"type": "string", "description": "Output directory, if mentioned"},
            "date_filter":      {"type": "string", "description": "Date or time filter, if mentioned"},
        },
    },
}


def extract_intent_from_message(message: str, existing: FileProcessingIntent) -> FileProcessingIntent:
    """Extract intent fields from user message, preserving existing known fields."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        tools=[EXTRACT_TOOL],
        tool_choice={"type": "any"},
        messages=[{"role": "user", "content": message}],
    )

    new_fields = {}
    for block in response.content:
        if block.type == "tool_use" and block.name == "extract_intent":
            new_fields = {k: v for k, v in block.input.items() if v}

    # Merge: existing fields take precedence unless overridden
    merged = existing.model_dump()
    for k, v in new_fields.items():
        if merged.get(k) is None and v:
            merged[k] = v

    return FileProcessingIntent(**merged)


def build_consolidated_question(intent: FileProcessingIntent) -> str:
    """Ask for ALL missing required info in one question."""
    missing = intent.missing_fields()
    required_missing = [f for f in missing if f in ("source_directory", "output_format")]

    if not required_missing:
        return ""

    questions = {
        "source_directory": "which directory contains the files",
        "output_format":    "what output format (CSV, JSON, or Parquet)",
    }
    parts = [questions[f] for f in required_missing if f in questions]
    return "To process your files, I need: " + " and ".join(parts) + "?"


def run_file_processing_agent():
    intent = FileProcessingIntent()
    history = []
    max_clarification_turns = 2  # hard limit on clarification loops
    clarification_turns = 0

    print("Agent: What files would you like to process?")

    while True:
        user_input = input("You: ").strip()
        if not user_input:
            continue

        history.append({"role": "user", "content": user_input})
        intent = extract_intent_from_message(user_input, intent)

        print(f"  [Intent so far] {intent.model_dump(exclude_none=True)}")

        if intent.is_complete() or clarification_turns >= max_clarification_turns:
            # Execute with what we have, using defaults for missing fields
            final_intent = intent.model_copy(update={
                "output_format": intent.output_format or "csv",
                "source_directory": intent.source_directory or "./data",
                "date_filter": intent.date_filter or "today",
            })
            print(f"\nAgent: Processing files with: {final_intent.model_dump()}")
            print("Agent: Done! Processed 14 files.")
            break
        else:
            question = build_consolidated_question(intent)
            if question:
                clarification_turns += 1
                print(f"Agent: {question}")
            else:
                print("Agent: Processing your files now...")
                break


# Simulate
if __name__ == "__main__":
    run_file_processing_agent()
```

**Expected Token Savings**: Consolidating all missing-field questions into one message halves the clarification round-trips. Hard turn limit prevents infinite loops.
**Environment**: Pydantic + tool use. Runs interactively.

---

### Option 2: Intent Confidence Threshold — Act Below Threshold, Ask Above

If confidence is high enough, act with best-guess interpretation. Only ask when confidence is low.

```python
import json
import anthropic
from dataclasses import dataclass

client = anthropic.Anthropic()


@dataclass
class InterpretedIntent:
    action: str
    parameters: dict
    confidence: float      # 0.0–1.0
    interpretation: str    # human-readable explanation of what the agent understood
    alternatives: list[str]  # other possible interpretations

    @property
    def should_confirm(self) -> bool:
        return self.confidence < 0.7

    @property
    def is_too_ambiguous(self) -> bool:
        return self.confidence < 0.4


INTERPRET_TOOL = {
    "name": "interpret_request",
    "description": "Interpret a potentially ambiguous user request.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action":         {"type": "string", "description": "Primary action inferred (e.g., 'send_report', 'process_files')"},
            "parameters":     {"type": "object", "description": "Inferred parameter values"},
            "confidence":     {"type": "number", "description": "Confidence 0.0–1.0"},
            "interpretation": {"type": "string", "description": "Human-readable summary of interpretation"},
            "alternatives":   {"type": "array", "items": {"type": "string"}, "description": "Other possible interpretations"},
        },
        "required": ["action", "parameters", "confidence", "interpretation", "alternatives"],
    },
}


def interpret_request(user_message: str) -> InterpretedIntent:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        tools=[INTERPRET_TOOL],
        tool_choice={"type": "any"},
        system=(
            "Interpret the user's request. Infer as much as possible from context. "
            "Set confidence=1.0 for unambiguous requests, 0.5 for partially clear, "
            "0.2 for very ambiguous."
        ),
        messages=[{"role": "user", "content": user_message}],
    )

    for block in response.content:
        if block.type == "tool_use":
            inp = block.input
            return InterpretedIntent(
                action=inp["action"],
                parameters=inp.get("parameters", {}),
                confidence=inp["confidence"],
                interpretation=inp["interpretation"],
                alternatives=inp.get("alternatives", []),
            )

    return InterpretedIntent("unknown", {}, 0.0, "Could not interpret", [])


def execute_intent(intent: InterpretedIntent) -> str:
    return f"Executed: {intent.action} with {intent.parameters}"


def run_agent(user_message: str) -> str:
    intent = interpret_request(user_message)
    print(f"  [Confidence: {intent.confidence:.0%}] {intent.interpretation}")

    if intent.is_too_ambiguous:
        # Ask a single focused question, offering alternatives
        alts = "\n".join(f"  {i+1}. {a}" for i, a in enumerate(intent.alternatives[:3]))
        return (
            f"I'm not sure what you'd like to do. Did you mean:\n{alts}\n\n"
            "Or please describe what you'd like in more detail."
        )
    elif intent.should_confirm:
        return (
            f"I'll proceed with: {intent.interpretation}\n"
            f"(Say 'stop' if that's wrong, otherwise I'll continue.)"
        )
    else:
        result = execute_intent(intent)
        return f"{result}\n\n({intent.interpretation})"


# Tests
for msg in [
    "Run the report.",                          # ambiguous
    "Send the Q1 sales report to the team.",   # clear
    "Do the thing from last week.",            # very ambiguous
]:
    print(f"\nUser: {msg}")
    print(f"Agent: {run_agent(msg)}")
```

**Expected Token Savings**: High-confidence requests execute immediately (0 clarification turns). Only ambiguous requests ask — at most once.
**Environment**: Tool-based intent interpretation. Works with Haiku for cost efficiency.

---

### Option 3: Clarification Budget Enforcer

Give the agent a fixed budget of 1–2 clarification questions. After the budget is spent, act with defaults.

```python
import anthropic

client = anthropic.Anthropic()

CLARIFICATION_BUDGET = 1   # max clarification turns before acting with defaults

SYSTEM = """You are a helpful assistant with a strict clarification budget.

CLARIFICATION RULES:
1. You may ask AT MOST {budget} clarifying question(s) per task.
2. After your budget is spent, you MUST act on your best interpretation — never ask again.
3. When asking, consolidate ALL unknowns into a single question.
4. When acting under uncertainty, state your interpretation explicitly.
5. Never ask the same question twice in different forms.
6. If you have already asked {budget} clarifying question(s), your next response MUST begin with "Acting on my best interpretation:"

Your clarification budget for this conversation: {budget} question(s).
""".strip()


def run_bounded_agent(task: str):
    """Run agent with enforced clarification budget."""
    system = SYSTEM.format(budget=CLARIFICATION_BUDGET)
    messages = [{"role": "user", "content": task}]
    clarifications_used = 0

    print(f"Task: {task}\n")

    while True:
        # Inject budget status into system
        budget_note = (
            f"\n\n[BUDGET STATUS: {CLARIFICATION_BUDGET - clarifications_used} clarification(s) remaining]"
            if clarifications_used < CLARIFICATION_BUDGET
            else "\n\n[BUDGET EXHAUSTED: You MUST act now, no more questions allowed]"
        )

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system + budget_note,
            messages=messages,
        )

        reply = response.content[0].text
        print(f"Agent: {reply}\n")

        # Detect if agent asked a question
        is_question = "?" in reply and not reply.startswith("Acting on")

        if is_question and clarifications_used < CLARIFICATION_BUDGET:
            clarifications_used += 1
            user_input = input(f"You ({clarifications_used}/{CLARIFICATION_BUDGET} clarifications used): ").strip()
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "user", "content": user_input})
        else:
            # Agent acted (or budget exhausted)
            break


if __name__ == "__main__":
    run_bounded_agent("Process the data and generate the output.")
```

**Expected Token Savings**: Hard budget prevents unlimited clarification loops. Each saved clarification turn saves ~300–500 tokens.
**Environment**: System prompt enforcement. Works with any model.

---

### Option 4: Default-Fill Strategy with Explicit Assumption Declaration

When information is missing, apply sensible defaults and tell the user exactly what was assumed.

```python
import anthropic

client = anthropic.Anthropic()

SYSTEM = """You are a task execution assistant. Your job is to complete tasks, not to ask questions.

EXECUTION RULES:
1. When a request is ambiguous, infer the most reasonable interpretation based on context.
2. Apply sensible defaults for any missing parameters.
3. ALWAYS list your assumptions at the start of your response as: "Assumptions: ..."
4. Execute immediately after stating assumptions.
5. End with: "If any assumption was wrong, tell me and I'll re-run."

NEVER ask clarifying questions. ALWAYS act.
"""


def run_agent(user_message: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


# Test with ambiguous requests
ambiguous_requests = [
    "Export the records.",
    "Clean up the old files.",
    "Send the summary to the stakeholders.",
    "Regenerate everything from scratch.",
]

for req in ambiguous_requests:
    print(f"User: {req}")
    print(f"Agent: {run_agent(req)}\n{'─'*60}\n")
```

**Expected Token Savings**: Zero clarification turns. Users get a complete response with stated assumptions — they only need to correct wrong ones.
**Environment**: Pure system prompt. Most effective for task-oriented agents where "try and correct" is acceptable.

---

### Option 5: Multi-Turn Intent Tracker with Convergence Detection

Track intent fields across turns. Detect when new turns aren't adding new information — stop asking.

```python
import json
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()


@dataclass
class IntentTracker:
    known: dict = field(default_factory=dict)
    unknown: set = field(default_factory=set)
    turns_without_progress: int = 0
    MAX_STALE_TURNS: int = 2

    def update(self, new_fields: dict) -> int:
        """Returns number of newly resolved fields."""
        resolved = 0
        for k, v in new_fields.items():
            if v and k not in self.known:
                self.known[k] = v
                self.unknown.discard(k)
                resolved += 1
        return resolved

    def mark_unknown(self, fields: list[str]):
        for f in fields:
            if f not in self.known:
                self.unknown.add(f)

    def should_stop_asking(self) -> bool:
        return self.turns_without_progress >= self.MAX_STALE_TURNS

    def record_turn(self, resolved: int):
        if resolved == 0:
            self.turns_without_progress += 1
        else:
            self.turns_without_progress = 0


PARSE_TOOL = {
    "name": "parse_intent",
    "description": "Parse any explicit information from the user message.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action":      {"type": "string"},
            "target":      {"type": "string", "description": "What to act on"},
            "format":      {"type": "string"},
            "destination": {"type": "string"},
            "time_filter": {"type": "string"},
            "unknown_fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Fields that are still unclear after parsing",
            },
        },
    },
}


def parse_user_message(message: str) -> tuple[dict, list[str]]:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        tools=[PARSE_TOOL],
        tool_choice={"type": "any"},
        messages=[{"role": "user", "content": message}],
    )
    for block in response.content:
        if block.type == "tool_use":
            inp = block.input
            unknown = inp.pop("unknown_fields", [])
            known = {k: v for k, v in inp.items() if v}
            return known, unknown
    return {}, []


def execute_task(intent: IntentTracker) -> str:
    params = intent.known
    defaults = {
        "action": "process",
        "target": "recent files",
        "format": "csv",
        "destination": "./output",
        "time_filter": "today",
    }
    final = {**defaults, **params}
    return f"Executed: {final['action']} on {final['target']} → {final['destination']} ({final['format']})"


def run_convergent_agent():
    tracker = IntentTracker()
    messages = []
    MAX_TURNS = 5

    print("Agent: How can I help?")

    for turn in range(MAX_TURNS):
        user_input = input("You: ").strip()
        if not user_input:
            continue

        messages.append({"role": "user", "content": user_input})
        known_fields, unknown_fields = parse_user_message(user_input)
        tracker.mark_unknown(unknown_fields)
        resolved = tracker.update(known_fields)
        tracker.record_turn(resolved)

        print(f"  [Known: {list(tracker.known.keys())} | Unknown: {list(tracker.unknown)} | Stale turns: {tracker.turns_without_progress}]")

        if not tracker.unknown or tracker.should_stop_asking():
            result = execute_task(tracker)
            print(f"Agent: {result}")
            break
        else:
            # Ask only about still-unknown fields
            unknowns = list(tracker.unknown)[:2]  # max 2 fields per question
            field_questions = {
                "action": "what action to take",
                "target": "which files/records",
                "format": "the output format",
                "destination": "where to save the output",
                "time_filter": "the date range",
            }
            question_parts = [field_questions.get(f, f) for f in unknowns]
            question = "I still need: " + " and ".join(question_parts) + "."
            print(f"Agent: {question}")


if __name__ == "__main__":
    run_convergent_agent()
```

**Expected Token Savings**: Convergence detection stops clarification when user answers stop adding new information — prevents 3–5 stale turns.
**Environment**: Tool-based parsing with stale-turn counter. Automatic fallback to defaults after MAX_STALE_TURNS.

---

### Option 6: Disambiguation via Multiple Choice

Instead of open-ended clarification questions, offer concrete numbered options. Users answer in one character.

```python
import anthropic

client = anthropic.Anthropic()

DISAMBIGUATION_TOOL = {
    "name": "request_disambiguation",
    "description": "Request clarification using numbered multiple-choice options.",
    "input_schema": {
        "type": "object",
        "properties": {
            "question":    {"type": "string", "description": "The clarifying question"},
            "options":     {
                "type": "array",
                "items": {"type": "string"},
                "description": "2–4 concrete options for the user to choose from",
                "maxItems": 4,
            },
            "default_option": {"type": "integer", "description": "0-indexed default if user doesn't answer"},
        },
        "required": ["question", "options", "default_option"],
    },
}

SYSTEM = """You are a task assistant. When a request is ambiguous:
1. Use request_disambiguation to offer specific numbered choices (max 4 options).
2. Only ask ONE disambiguation question total.
3. After clarification (or if already clear), execute the task immediately.
4. Never ask follow-up questions after the first disambiguation."""


def run_agent():
    messages = []
    disambiguated = False

    print("Agent: What would you like to do?")
    user_input = input("You: ").strip()
    messages.append({"role": "user", "content": user_input})

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM,
            tools=[DISAMBIGUATION_TOOL],
            messages=messages,
        )

        tool_used = False
        for block in response.content:
            if block.type == "tool_use" and not disambiguated:
                tool_used = True
                disambiguated = True
                q = block.input["question"]
                opts = block.input["options"]
                default = block.input.get("default_option", 0)

                print(f"\nAgent: {q}")
                for i, opt in enumerate(opts):
                    marker = " (default)" if i == default else ""
                    print(f"  {i+1}. {opt}{marker}")

                choice_str = input("You (enter number or press Enter for default): ").strip()
                chosen_idx = default
                if choice_str.isdigit():
                    idx = int(choice_str) - 1
                    if 0 <= idx < len(opts):
                        chosen_idx = idx

                chosen = opts[chosen_idx]
                print(f"  [Selected: {chosen}]")

                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": block.id, "content": f"User selected: {chosen}"},
                ]})

        if not tool_used:
            # Agent gave a final text response
            reply = next((b.text for b in response.content if b.type == "text"), "")
            print(f"\nAgent: {reply}")
            break

        if disambiguated and not tool_used:
            break


if __name__ == "__main__":
    run_agent()
```

**Expected Token Savings**: Multiple-choice reduces clarification from open-ended back-and-forth (3–5 turns) to a single selection. Users answer with "1", "2", or Enter.
**Environment**: Tool-based. Works in CLI, chat UI, or API integration.

---

| Option | Strategy | Max Turns | User Effort | Best For |
|--------|---------|----------|------------|---------|
| 1 | Structured field extraction + gap analysis | 1–2 | Low | Forms/structured tasks |
| 2 | Confidence threshold | 0 or 1 | Minimal | General tasks, high LLM capability |
| 3 | Hard clarification budget | N (configurable) | Low | Any agent, budget enforcement |
| 4 | Default-fill + assumption declaration | 0 | None | Speed-critical, correctable tasks |
| 5 | Convergence detection | Bounded | Medium | Complex multi-field tasks |
| 6 | Multiple-choice disambiguation | 1 | Minimal | UX-focused, guided workflows |
