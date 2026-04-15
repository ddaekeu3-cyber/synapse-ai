---
layout: solution
title: "Agent Loses Track of Original Goal in Long Conversations"
category: context-window
description: "In long multi-turn conversations, the agent drifts from the user's original request, answering sub-questions while forgetting the overarching goal."
tags: [context-window, goal-tracking, long-conversation, memory, multi-turn]
---

## Symptom

The user asks a complex multi-part question. The agent begins addressing it, then progressively loses sight of the primary objective as the conversation grows:

```
Turn 1 — User: "Help me build a FastAPI application with auth, a PostgreSQL database,
               and Docker deployment. Start with the database schema."
Turn 2 — Agent: [discusses schema]
Turn 3 — User: "Great, what about indexes?"
Turn 4 — Agent: [discusses indexes]
...
Turn 12 — User: "Now let's handle errors"
Turn 13 — Agent: [discusses error handling in detail]
Turn 20 — User: "OK now what?"
Turn 21 — Agent: "We've covered error handling thoroughly! Is there anything else
                  you'd like to explore about error handling?"
# ← Agent forgot the original plan: auth + PostgreSQL + Docker
```

Root causes:
- Original goal mentioned once, buried under 20+ subsequent turns
- Sub-tasks feel "complete" and the agent doesn't track what's pending
- No persistent goal state — agent re-reads the entire context on each turn
- User's follow-up questions shift the agent's focus to sub-topics
- Long context dilutes the attention weight on the original request

---

## Root Cause

Attention in transformer models is not perfectly uniform across the context window. In very long conversations, the beginning of the conversation (where the original goal was stated) competes with more recent turns for the model's attention. The most recent few turns dominate the model's generation, causing progressive drift toward sub-topics.

The solution is to make the original goal structurally impossible to forget: track it explicitly in a persistent state, re-inject it at each turn, or summarize the task plan at regular intervals.

---

## Fix

### Option 1 — Goal Anchor Re-injection at Every Turn

Extract the original goal at conversation start and re-inject it into every subsequent prompt.

```python
import anthropic
import json

client = anthropic.Anthropic()

def extract_goal(first_message: str) -> str:
    """Use a fast model to extract the overarching goal from the first user message."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        system=(
            "Extract the user's primary goal from their message. "
            "Be concise — 1-3 sentences. Focus on the final outcome they want, "
            "not the specific first step they asked for."
        ),
        messages=[{"role": "user", "content": f"Extract the goal from: {first_message}"}]
    )
    return response.content[0].text.strip()

class GoalAnchoredConversation:
    """Conversation that re-injects the original goal at every turn."""

    def __init__(self, base_system: str):
        self.base_system = base_system
        self.history: list[dict] = []
        self.original_goal: str = ""
        self.completed_steps: list[str] = []

    def _build_system(self) -> str:
        if not self.original_goal:
            return self.base_system

        goal_block = f"""
## ORIGINAL USER GOAL (do not lose track of this)
{self.original_goal}

## Completed so far
{chr(10).join(f'✓ {s}' for s in self.completed_steps) if self.completed_steps else 'Nothing completed yet'}

## Your job
After addressing the user's current question, briefly remind them what remains
toward the original goal. Proactively offer to continue the plan.
"""
        return self.base_system + goal_block

    def chat(self, user_message: str) -> str:
        # Extract goal from first message
        if not self.history:
            self.original_goal = extract_goal(user_message)
            print(f"  [goal extracted] {self.original_goal[:100]}")

        self.history.append({"role": "user", "content": user_message})

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=self._build_system(),  # goal injected every turn
            messages=self.history,
        )

        reply = response.content[0].text
        self.history.append({"role": "assistant", "content": reply})
        return reply

    def mark_step_complete(self, step: str):
        self.completed_steps.append(step)

BASE_SYSTEM = "You are a senior software engineer helping build a complete application."

conv = GoalAnchoredConversation(BASE_SYSTEM)

turns = [
    "Help me build a FastAPI app with JWT auth, PostgreSQL, and Docker. Start with the DB schema.",
    "Great, what indexes should I add to the users table?",
    "What about partial indexes for performance?",
    "Now let's move to the next part",
]

for msg in turns:
    print(f"\nUser: {msg[:80]}")
    reply = conv.chat(msg)
    print(f"Agent: {reply[:300]}")
    print()
```

**Expected Token Savings:** Goal re-injection costs ~50 tokens/turn; prevents losing the user's primary intent which would require a full context restart or lengthy re-explanation.

**Environment:** Python 3.9+, `anthropic>=0.40.0`. Haiku goal extraction costs ~100 tokens once at session start.

---

### Option 2 — Structured Task Plan with Progress Tracking

Create an explicit task plan at conversation start and track progress step by step.

```python
import anthropic
import json
from dataclasses import dataclass, field
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class TaskStep:
    id: str
    description: str
    status: str = "pending"  # pending, in_progress, done, skipped
    subtasks: list[str] = field(default_factory=list)
    notes: str = ""

PLAN_TOOL = {
    "name": "create_task_plan",
    "description": "Create a structured plan for the user's request. Call this at the start of complex tasks.",
    "input_schema": {
        "type": "object",
        "properties": {
            "goal": {"type": "string", "description": "The user's ultimate goal in one sentence"},
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "description": {"type": "string"},
                        "subtasks": {"type": "array", "items": {"type": "string"}}
                    },
                    "required": ["id", "description"]
                },
                "description": "Ordered list of steps to achieve the goal"
            }
        },
        "required": ["goal", "steps"]
    }
}

UPDATE_TOOL = {
    "name": "update_task_status",
    "description": "Update the status of a task step. Call this when completing or skipping a step.",
    "input_schema": {
        "type": "object",
        "properties": {
            "step_id": {"type": "string"},
            "status": {"type": "string", "enum": ["in_progress", "done", "skipped"]},
            "notes": {"type": "string", "description": "Optional notes about completion"}
        },
        "required": ["step_id", "status"]
    }
}

class TaskPlanManager:
    def __init__(self):
        self.goal: str = ""
        self.steps: list[TaskStep] = []

    def create_plan(self, goal: str, steps_data: list[dict]) -> str:
        self.goal = goal
        self.steps = [
            TaskStep(
                id=s["id"],
                description=s["description"],
                subtasks=s.get("subtasks", [])
            )
            for s in steps_data
        ]
        return f"Plan created with {len(self.steps)} steps."

    def update_step(self, step_id: str, status: str, notes: str = "") -> str:
        for step in self.steps:
            if step.id == step_id:
                step.status = status
                step.notes = notes
                return f"Step '{step_id}' marked as {status}."
        return f"Step '{step_id}' not found."

    def render_plan(self) -> str:
        if not self.steps:
            return "No plan created yet."

        status_icons = {"pending": "○", "in_progress": "→", "done": "✓", "skipped": "⊘"}
        lines = [f"Goal: {self.goal}", ""]
        for step in self.steps:
            icon = status_icons.get(step.status, "?")
            lines.append(f"  {icon} [{step.id}] {step.description}")
            if step.status == "done" and step.notes:
                lines.append(f"       Note: {step.notes}")
        pending = [s for s in self.steps if s.status == "pending"]
        if pending:
            lines.append(f"\nNext up: [{pending[0].id}] {pending[0].description}")
        else:
            lines.append("\nAll steps complete!")
        return "\n".join(lines)

    def inject_plan_context(self) -> str:
        """Text to inject into system prompt."""
        if not self.steps:
            return ""
        return f"\n\n## Current Task Plan\n{self.render_plan()}\n"

plan_manager = TaskPlanManager()

def handle_tool_call(tool_name: str, tool_input: dict) -> str:
    if tool_name == "create_task_plan":
        result = plan_manager.create_plan(tool_input["goal"], tool_input["steps"])
        print(f"\n  [Plan created]\n{plan_manager.render_plan()}\n")
        return result
    if tool_name == "update_task_status":
        result = plan_manager.update_step(
            tool_input["step_id"], tool_input["status"], tool_input.get("notes", "")
        )
        print(f"\n  [Plan updated]\n{plan_manager.render_plan()}\n")
        return result
    return f"Unknown tool: {tool_name}"

BASE_SYSTEM = "You are a software engineering assistant. For complex tasks, create a plan first and update it as you progress."

messages = [
    {
        "role": "user",
        "content": (
            "Help me build a FastAPI application with JWT authentication, "
            "PostgreSQL database, and Docker deployment. Where should we start?"
        )
    }
]

turns_to_simulate = [
    "The schema looks good. What about indexes?",
    "Got it. Now let's work on JWT auth",
    "That's the auth done. What's left?",
]

def get_system_with_plan() -> str:
    return BASE_SYSTEM + plan_manager.inject_plan_context()

# Initial turn
for turn_idx in range(5):
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        system=get_system_with_plan(),
        tools=[PLAN_TOOL, UPDATE_TOOL],
        messages=messages,
    )

    tool_results = []
    for block in resp.content:
        if block.type == "tool_use":
            result = handle_tool_call(block.name, block.input)
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

    messages.append({"role": "assistant", "content": resp.content})
    if tool_results:
        messages.append({"role": "user", "content": tool_results})
        continue

    if resp.stop_reason == "end_turn":
        print(f"Agent: {resp.content[0].text[:300] if resp.content else ''}")
        if turn_idx < len(turns_to_simulate):
            next_msg = turns_to_simulate[turn_idx]
            print(f"\nUser: {next_msg}")
            messages.append({"role": "user", "content": next_msg})
        else:
            break
```

**Expected Token Savings:** Explicit plan tracking prevents redoing completed work (~200-500 tokens per redo). More importantly, prevents the user from having to re-explain context.

**Environment:** Python 3.9+, `anthropic>=0.40.0`.

---

### Option 3 — Rolling Summary with Goal Preservation

Periodically summarize the conversation, always preserving the original goal in the summary.

```python
import anthropic

client = anthropic.Anthropic()

def summarize_with_goal(
    history: list[dict],
    original_goal: str,
    remaining_steps: list[str],
) -> str:
    """Summarize conversation history while explicitly preserving goal and pending steps."""
    history_text = "\n".join(
        f"{'User' if m['role'] == 'user' else 'Assistant'}: {str(m['content'])[:200]}"
        for m in history
    )

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system="You are a conversation summarizer. Always preserve goal context.",
        messages=[{
            "role": "user",
            "content": (
                f"Original goal: {original_goal}\n"
                f"Remaining to complete: {remaining_steps}\n\n"
                f"Conversation so far:\n{history_text}\n\n"
                "Write a concise summary that:\n"
                "1. Restates the ORIGINAL GOAL clearly\n"
                "2. Lists what has been COMPLETED so far\n"
                "3. Lists what REMAINS to be done\n"
                "4. Notes any key decisions or code produced\n"
                "Keep under 200 words."
            )
        }]
    )
    return response.content[0].text

class GoalPreservingConversation:
    SUMMARIZE_EVERY_N_TURNS = 6

    def __init__(self, system: str):
        self.system = system
        self.history: list[dict] = []
        self.original_goal: str = ""
        self.remaining_steps: list[str] = []
        self.summaries: list[str] = []
        self.turn_count = 0

    def set_goal(self, goal: str, steps: list[str]):
        self.original_goal = goal
        self.remaining_steps = steps.copy()

    def complete_step(self, step: str):
        if step in self.remaining_steps:
            self.remaining_steps.remove(step)

    def _maybe_summarize(self):
        """Summarize and compress history if too long."""
        if len(self.history) >= self.SUMMARIZE_EVERY_N_TURNS:
            summary = summarize_with_goal(
                self.history, self.original_goal, self.remaining_steps
            )
            self.summaries.append(summary)
            print(f"\n  [Conversation summarized — {len(self.history)} turns compressed]")
            print(f"  Summary: {summary[:150]}...")
            # Keep only the last 2 turns in history; rest is in summary
            self.history = self.history[-2:]

    def _build_messages(self, user_message: str) -> list[dict]:
        """Build message list with summary context if available."""
        messages = []

        if self.summaries:
            summary_context = "\n\n---\n".join(self.summaries)
            messages.append({
                "role": "user",
                "content": f"[Previous conversation summary:\n{summary_context}\n]"
            })
            messages.append({
                "role": "assistant",
                "content": "I have the context from our previous discussion."
            })

        messages.extend(self.history)
        messages.append({"role": "user", "content": user_message})
        return messages

    def _build_system(self) -> str:
        goal_reminder = ""
        if self.original_goal:
            remaining = "\n".join(f"  - {s}" for s in self.remaining_steps)
            goal_reminder = (
                f"\n\n## ACTIVE TASK PLAN\n"
                f"Goal: {self.original_goal}\n"
                f"Remaining steps:\n{remaining if remaining else '  (none — all done!)'}\n\n"
                f"After each response, proactively guide toward the next remaining step."
            )
        return self.system + goal_reminder

    def chat(self, user_message: str) -> str:
        self._maybe_summarize()
        messages = self._build_messages(user_message)

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            system=self._build_system(),
            messages=messages,
        )

        reply = response.content[0].text
        self.history.append({"role": "user", "content": user_message})
        self.history.append({"role": "assistant", "content": reply})
        self.turn_count += 1
        return reply

# Demo
conv = GoalPreservingConversation(
    system="You are a senior backend engineer helping build a complete web application."
)
conv.set_goal(
    goal="Build a FastAPI application with JWT auth, PostgreSQL, and Docker deployment",
    steps=["database schema", "JWT authentication", "API endpoints", "Docker setup", "testing"]
)

conversation = [
    "Help me build a FastAPI app with JWT auth, PostgreSQL, and Docker. Start with the DB.",
    "What indexes do we need?",
    "Should I use UUID or serial for primary keys?",
    "OK schema looks good. What's next?",
    "Let's do the auth. Show me the JWT implementation.",
    "How do I handle token refresh?",
    "What about the Docker setup?",  # After 6 turns, should summarize and stay on track
]

for msg in conversation:
    print(f"\nUser: {msg[:70]}")
    reply = conv.chat(msg)
    print(f"Agent: {reply[:250]}")
    print(f"       [Remaining: {conv.remaining_steps}]")
```

**Expected Token Savings:** Rolling summaries compress 6 turns (~3,000 tokens) into ~200 tokens, enabling much longer goal-preserving conversations without context overflow.

**Environment:** Python 3.9+, `anthropic>=0.40.0`. Haiku summarization costs ~100-150 tokens every N turns.

---

### Option 4 — Goal State in Tool Schema (Persistent Tool-Based Memory)

Use a tool to store and retrieve goal state, making it persistent and queryable.

```python
import anthropic
import json

client = anthropic.Anthropic()

# Goal state persisted outside the conversation history
goal_state = {
    "goal": "",
    "total_steps": [],
    "completed": [],
    "in_progress": "",
    "blocked_on": "",
    "key_decisions": [],
}

GOAL_TOOLS = [
    {
        "name": "set_goal",
        "description": "Set the user's primary goal and decompose it into steps. Call at the start of complex tasks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "steps": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["goal", "steps"]
        }
    },
    {
        "name": "advance_goal",
        "description": "Mark current step as done and identify the next step. Call after completing each step.",
        "input_schema": {
            "type": "object",
            "properties": {
                "completed_step": {"type": "string"},
                "key_decision": {"type": "string", "description": "Any important decision made (optional)"},
                "next_step": {"type": "string", "description": "What to work on next"},
            },
            "required": ["completed_step", "next_step"]
        }
    },
    {
        "name": "check_goal_progress",
        "description": "Get current goal state — call to see what's done and what's next.",
        "input_schema": {"type": "object", "properties": {}}
    }
]

def handle_goal_tool(tool_name: str, tool_input: dict) -> str:
    global goal_state

    if tool_name == "set_goal":
        goal_state["goal"] = tool_input["goal"]
        goal_state["total_steps"] = tool_input["steps"]
        goal_state["in_progress"] = tool_input["steps"][0] if tool_input["steps"] else ""
        return json.dumps({"ok": True, "message": f"Goal set. Starting with: {goal_state['in_progress']}"})

    if tool_name == "advance_goal":
        step = tool_input["completed_step"]
        if step not in goal_state["completed"]:
            goal_state["completed"].append(step)
        if tool_input.get("key_decision"):
            goal_state["key_decisions"].append(tool_input["key_decision"])
        goal_state["in_progress"] = tool_input["next_step"]
        remaining = [s for s in goal_state["total_steps"]
                     if s not in goal_state["completed"] and s != goal_state["in_progress"]]
        return json.dumps({
            "ok": True,
            "completed": goal_state["completed"],
            "now_working_on": goal_state["in_progress"],
            "remaining_after_this": remaining,
        })

    if tool_name == "check_goal_progress":
        remaining = [s for s in goal_state["total_steps"]
                     if s not in goal_state["completed"]]
        return json.dumps({
            "goal": goal_state["goal"],
            "completed": goal_state["completed"],
            "in_progress": goal_state["in_progress"],
            "remaining": remaining,
            "key_decisions": goal_state["key_decisions"],
            "pct_complete": len(goal_state["completed"]) / max(len(goal_state["total_steps"]), 1) * 100,
        })

    return json.dumps({"error": f"Unknown tool: {tool_name}"})

SYSTEM = (
    "You are a methodical software engineer. For complex tasks:\n"
    "1. Use set_goal to record the plan at the start\n"
    "2. Use advance_goal when finishing each step\n"
    "3. Use check_goal_progress if you lose track of where you are\n"
    "Always tell the user what's been completed and what's next."
)

def run_goal_tracked(conversation_turns: list[str]) -> None:
    messages = []

    for user_msg in conversation_turns:
        print(f"\nUser: {user_msg[:80]}")
        messages.append({"role": "user", "content": user_msg})

        while True:
            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=600,
                system=SYSTEM,
                tools=GOAL_TOOLS,
                messages=messages,
            )

            messages.append({"role": "assistant", "content": resp.content})
            tool_calls = [b for b in resp.content if b.type == "tool_use"]

            if not tool_calls:
                for block in resp.content:
                    if hasattr(block, "text"):
                        print(f"Agent: {block.text[:300]}")
                break

            results = []
            for block in tool_calls:
                result = handle_goal_tool(block.name, block.input)
                print(f"  [tool: {block.name}] → {result[:120]}")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
            messages.append({"role": "user", "content": results})

run_goal_tracked([
    "I need to build a REST API with user auth, database layer, and API docs. Where to start?",
    "The auth is done. Now let's do the database",
    "Database looks good. What about docs?",
    "Can you remind me what we're building overall?",
    "What's left to do?",
])
```

**Expected Token Savings:** Tool-based goal persistence costs ~50 tokens per advance_goal call but eliminates re-explanation overhead when the agent loses context (~500-1,000 tokens each time).

**Environment:** Python 3.9+, `anthropic>=0.40.0`.

---

### Option 5 — Hierarchical Context: Goal → Current Step → Turn

Structure messages to maintain a hierarchy: global goal → current step → current exchange.

```python
import anthropic

client = anthropic.Anthropic()

def build_hierarchical_system(
    global_goal: str,
    all_steps: list[str],
    current_step: str,
    completed_steps: list[str],
    artifacts: dict[str, str],
) -> str:
    completed_str = "\n".join(f"  ✓ {s}" for s in completed_steps) if completed_steps else "  (none yet)"
    pending = [s for s in all_steps if s not in completed_steps and s != current_step]
    pending_str = "\n".join(f"  ○ {s}" for s in pending) if pending else "  (none — almost done!)"
    artifacts_str = (
        "\n".join(f"  • {name}: {desc}" for name, desc in artifacts.items())
        if artifacts else "  (none yet)"
    )

    return f"""You are a methodical software architect.

═══════════════════════════════════════
GLOBAL GOAL
═══════════════════════════════════════
{global_goal}

COMPLETED STEPS
{completed_str}

→ CURRENT STEP: {current_step}

REMAINING STEPS
{pending_str}

PRODUCED ARTIFACTS
{artifacts_str}
═══════════════════════════════════════

After EVERY response:
1. Complete the current step fully
2. Announce what you produced
3. Suggest the next step toward the global goal
Do NOT wander into other steps without completing the current one."""

class HierarchicalConversation:
    def __init__(self, goal: str, steps: list[str]):
        self.goal = goal
        self.steps = steps
        self.completed: list[str] = []
        self.current_step = steps[0] if steps else ""
        self.artifacts: dict[str, str] = {}
        self.history: list[dict] = []

    def advance(self, artifact_name: str = None, artifact_desc: str = None):
        """Mark current step as complete and move to next."""
        if self.current_step:
            self.completed.append(self.current_step)
        if artifact_name and artifact_desc:
            self.artifacts[artifact_name] = artifact_desc
        remaining = [s for s in self.steps if s not in self.completed]
        self.current_step = remaining[0] if remaining else "Complete!"

    def chat(self, user_message: str) -> str:
        system = build_hierarchical_system(
            self.goal, self.steps, self.current_step,
            self.completed, self.artifacts
        )
        self.history.append({"role": "user", "content": user_message})

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            system=system,
            messages=self.history,
        )

        reply = response.content[0].text
        self.history.append({"role": "assistant", "content": reply})
        return reply

conv = HierarchicalConversation(
    goal="Build production-ready FastAPI service with JWT auth, PostgreSQL, Redis caching, and Docker",
    steps=[
        "PostgreSQL schema and migrations",
        "JWT authentication middleware",
        "Core API endpoints",
        "Redis caching layer",
        "Docker and docker-compose setup",
        "API documentation",
    ]
)

turns = [
    "Let's start building. What's first?",
    "Show me the users table specifically",
    "That's great! Auth setup next?",
]

for msg in turns:
    print(f"\nUser: {msg}")
    reply = conv.chat(msg)
    print(f"Agent: {reply[:400]}")
    print(f"  [Step: {conv.current_step} | Done: {conv.completed}]")

# After completing schema step:
conv.advance("users_schema.sql", "PostgreSQL schema with users, sessions, roles tables")
print(f"\n  [Advanced to: {conv.current_step}]")
```

**Expected Token Savings:** Hierarchical context adds ~200 tokens/turn but eliminates goal drift that would otherwise require re-explaining the project from scratch (~500-2,000 tokens per drift incident).

**Environment:** Python 3.9+, `anthropic>=0.40.0`.

---

### Option 6 — Automatic Goal Drift Detection

Monitor responses for signs of goal drift and inject a course-correction when detected.

```python
import anthropic
import re

client = anthropic.Anthropic()

def detect_goal_drift(
    response_text: str,
    original_goal_keywords: list[str],
    current_step: str,
    min_keyword_presence: float = 0.15,
) -> tuple[bool, str]:
    """
    Detect if a response has drifted from the goal.
    Returns (is_drifted, reason).
    """
    response_lower = response_text.lower()

    # Check if goal keywords appear
    keyword_hits = sum(1 for kw in original_goal_keywords if kw.lower() in response_lower)
    keyword_ratio = keyword_hits / max(len(original_goal_keywords), 1)

    # Check for signs of open-ended wandering
    drift_phrases = [
        "is there anything else", "what would you like to explore",
        "feel free to ask", "happy to help with anything",
        "any other questions", "let me know if you need",
    ]
    has_drift_phrase = any(phrase in response_lower for phrase in drift_phrases)

    # Check if current step is mentioned
    step_mentioned = current_step.lower() in response_lower

    if keyword_ratio < min_keyword_presence and not step_mentioned:
        return True, f"Goal keywords absent ({keyword_hits}/{len(original_goal_keywords)})"
    if has_drift_phrase and not step_mentioned:
        return True, "Open-ended closing without next step guidance"

    return False, ""

def course_correct(
    messages: list[dict],
    original_goal: str,
    current_step: str,
    remaining_steps: list[str],
    system: str,
    tools: list,
) -> str:
    """Generate a course-corrected response."""
    correction = (
        f"\n\n[COURSE CORRECTION NEEDED]\n"
        f"You drifted from the plan. Refocus:\n"
        f"• Original goal: {original_goal}\n"
        f"• Current step: {current_step}\n"
        f"• Remaining: {', '.join(remaining_steps)}\n"
        f"Acknowledge where we are and guide toward the next step."
    )

    augmented = messages.copy()
    if augmented and augmented[-1]["role"] == "user":
        augmented[-1] = {
            **augmented[-1],
            "content": str(augmented[-1]["content"]) + correction,
        }

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=system,
        messages=augmented,
    )
    return resp.content[0].text

SYSTEM = "You are a project-focused assistant. Always keep the user's primary goal in view."
GOAL = "Build a complete user authentication system with registration, login, JWT, and password reset"
GOAL_KEYWORDS = ["auth", "jwt", "registration", "login", "password", "token", "user"]
STEPS = ["user registration endpoint", "login and JWT generation", "token refresh", "password reset flow"]
current_step = STEPS[0]
remaining = STEPS[1:]

messages = []
turns = [
    "Help me build user auth with registration, login, JWT, and password reset",
    "Tell me more about bcrypt hashing",
    "What's the history of password hashing algorithms?",  # drifting
    "That's interesting. What about argon2?",              # more drift
]

for msg in turns:
    print(f"\nUser: {msg}")
    messages.append({"role": "user", "content": msg})

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        system=SYSTEM,
        messages=messages,
    )
    reply = resp.content[0].text

    # Check for drift
    drifted, reason = detect_goal_drift(reply, GOAL_KEYWORDS, current_step)
    if drifted:
        print(f"  [DRIFT DETECTED: {reason}] — course correcting...")
        reply = course_correct(messages, GOAL, current_step, remaining, SYSTEM, [])
        print(f"  [Course corrected]")

    messages.append({"role": "assistant", "content": reply})
    print(f"Agent: {reply[:300]}")
```

**Expected Token Savings:** Drift detection adds ~50ms; course correction costs ~300 tokens but prevents multi-turn drift episodes that waste 2,000-5,000 tokens total.

**Environment:** Python 3.9+, `re`, `anthropic>=0.40.0`.

---

## Comparison

| Option | Prevention | Detects Drift | Scales to Long Sessions | Complexity |
|--------|-----------|--------------|------------------------|------------|
| 1 — Goal Re-injection | Every turn | No | Medium | Low |
| 2 — Task Plan Tool | Structural | Partial | Yes | Medium |
| 3 — Rolling Summary | Compression | No | Yes (bounded) | Medium |
| 4 — Tool-Based State | Persistent | No | Yes | Medium |
| 5 — Hierarchical System | Every turn | No | Medium | Low |
| 6 — Drift Detection | Reactive | Yes | Yes | Medium |

**Start with Option 1** (goal re-injection) — it's 10 lines of code and immediately effective. Add **Option 3** (rolling summary) for sessions expected to exceed 20 turns. Use **Option 2** (task plan tool) for complex projects with many sequential steps.
