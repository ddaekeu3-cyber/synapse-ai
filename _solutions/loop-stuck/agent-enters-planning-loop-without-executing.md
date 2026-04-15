---
layout: solution
title: "Agent enters planning loop without executing"
category: loop-stuck
description: "Agent repeatedly re-plans, refines, or reconsiders its approach without ever calling a tool or producing output. Each turn generates a new plan that spawns another planning turn — the agent thinks endlessly but never acts. Execution deadlines, plan-once guards, and forced tool selection break the loop."
tags: [loop-stuck, planning, execution, tool-use, deadlock, prompt-engineering, meta-cognition]
---

## Symptom

The agent's response is always some variant of "Let me think about the best approach...", "Before I proceed, I should consider...", "My plan is: step 1... step 2... step 3... Let me re-evaluate this plan first." Ten turns pass. No tool has been called. No output has been produced. The user asks "are you going to do anything?" and the agent produces another plan.

## Root Cause

The model's RLHF training rewards careful, thoughtful responses — "plan before acting" is a strong prior. Without a hard constraint on planning rounds, the model can get stuck in a loop where each "plan" mentions the need to verify the plan, which triggers another planning turn. This is especially common with complex tasks where uncertainty is high and the model treats "planning" as reducing uncertainty before committing to action.

## Fix

Enforce an execution deadline: allow exactly N planning turns before forcing a tool call. Detect planning-only responses and inject a "stop planning, start doing" instruction. Use `tool_choice: {"type": "any"}` to force at least one tool call per turn. Track whether progress is being made by checking if the tool call count has increased.

---

### Option 1 — Execution deadline: N plans then force action

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

TOOLS = [
    {
        "name": "search_web",
        "description": "Search for information online.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["filename", "content"],
        },
    },
]

PLANNING_PHRASES = [
    "let me think", "let me consider", "i should first", "my plan is",
    "step 1:", "before i proceed", "i need to", "i'll start by planning",
    "let me outline", "let me re-evaluate", "let me reconsider",
]


def is_planning_only(response_text: str) -> bool:
    """Detect if a response is all planning with no action."""
    lower = response_text.lower()
    has_planning = any(phrase in lower for phrase in PLANNING_PHRASES)
    is_long = len(response_text) > 200
    return has_planning and is_long


def run_agent(user_message: str, max_planning_turns: int = 2) -> str:
    messages = [{"role": "user", "content": user_message}]
    planning_turns = 0
    tool_calls_made = 0

    for turn in range(10):
        # After max_planning_turns, force a tool call
        force_tool = planning_turns >= max_planning_turns
        kwargs = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 1024,
            "tools": TOOLS,
            "messages": messages,
        }
        if force_tool:
            kwargs["tool_choice"] = {"type": "any"}  # must call a tool this turn
            print(f"[Turn {turn}] Planning limit reached — forcing tool call")
        else:
            print(f"[Turn {turn}] Planning turns: {planning_turns}/{max_planning_turns}")

        response = client.messages.create(**kwargs)
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            text = response.content[0].text if response.content else ""
            if is_planning_only(text):
                planning_turns += 1
                print(f"[Turn {turn}] Detected planning-only response")
                # Inject a nudge to stop planning and start acting
                messages.append({
                    "role": "user",
                    "content": "Stop planning and start executing. Call a tool now.",
                })
            else:
                return text

        elif response.stop_reason == "tool_use":
            tool_calls_made += 1
            planning_turns = 0   # reset planning counter on tool call
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    print(f"[Turn {turn}] Tool call: {block.name}({block.input})")
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"[Result of {block.name}]",
                    })
            messages.append({"role": "user", "content": results})

    return f"Agent completed with {tool_calls_made} tool calls"


result = run_agent(
    "Research the top 5 Python web frameworks and write a comparison report to frameworks.md"
)
print(result)
```

**Expected Token Savings:** Each prevented planning turn saves ~400–800 tokens; for a task that would otherwise loop for 10 planning turns, forcing execution at turn 2 saves ~3200–6400 tokens.
**Environment:** Any task-oriented agent with tools; the planning deadline is the lowest-cost intervention — two lines of code (counter + `tool_choice`) prevent indefinite loops.

---

### Option 2 — Progress tracking: detect zero-progress turns

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

TOOLS = [
    {
        "name": "execute_task",
        "description": "Execute a specific task step.",
        "input_schema": {
            "type": "object",
            "properties": {
                "step": {"type": "string"},
                "action": {"type": "string"},
            },
            "required": ["step", "action"],
        },
    },
]


def run_agent_with_progress(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    tool_call_count = 0
    zero_progress_turns = 0

    SYSTEM = (
        "You are a task execution agent. "
        "For every response, you MUST either call a tool OR return a final answer. "
        "Never output a response that is only planning or reasoning without taking action. "
        "If you find yourself writing a plan, immediately execute the first step of that plan."
    )

    for turn in range(15):
        calls_before = tool_call_count

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            text = next((b.text for b in response.content if hasattr(b, "text")), "")
            if tool_call_count == calls_before:
                zero_progress_turns += 1
                print(f"[Progress] Zero-progress turn #{zero_progress_turns}")
                if zero_progress_turns >= 3:
                    print("[Progress] 3 consecutive zero-progress turns — aborting")
                    return f"Agent stuck in planning loop after {turn} turns. Last output: {text[:200]}"
                # Inject a harder nudge
                messages.append({
                    "role": "user",
                    "content": (
                        "You have not called any tools in the last turn. "
                        "You must call execute_task NOW. Do not plan — execute."
                    ),
                })
            else:
                zero_progress_turns = 0
                return text

        elif response.stop_reason == "tool_use":
            zero_progress_turns = 0
            for block in response.content:
                if block.type == "tool_use":
                    tool_call_count += 1
                    print(f"[Tool:{tool_call_count}] {block.name}({block.input})")
                    messages.append({
                        "role": "user",
                        "content": [{
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"Step '{block.input.get('step')}' completed.",
                        }],
                    })

    return f"Max turns reached. Tool calls made: {tool_call_count}"


result = run_agent_with_progress(
    "Set up a Python project with Flask, SQLAlchemy, and a Dockerfile"
)
print(result)
```

**Expected Token Savings:** Zero-progress detection aborts after 3 stuck turns, saving all remaining planned iterations (~6–12 turns × ~600 tokens = 3600–7200 tokens for a deeply stuck agent).
**Environment:** Long-horizon task agents; progress tracking is more robust than a simple planning detector because it catches any stuck pattern, not just explicit planning language.

---

### Option 3 — First-turn forced tool: eliminate planning on simple tasks

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

# For tasks where the right tool is obvious, skip the planning turn entirely
TASK_TOOL_MAP = {
    "search": "web_search",
    "find": "web_search",
    "look up": "web_search",
    "write": "write_file",
    "create file": "write_file",
    "read": "read_file",
    "execute": "run_command",
    "run": "run_command",
}

TOOLS = [
    {
        "name": "web_search",
        "description": "Search the web.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "write_file",
        "description": "Write to a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
]


def detect_likely_tool(user_message: str) -> str | None:
    lower = user_message.lower()
    for keyword, tool in TASK_TOOL_MAP.items():
        if keyword in lower:
            return tool
    return None


def run_agent_first_turn_forced(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    likely_tool = detect_likely_tool(user_message)

    for turn in range(10):
        kwargs = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 1024,
            "tools": TOOLS,
            "messages": messages,
        }

        # On turn 0, if we know the right tool, force it immediately
        if turn == 0 and likely_tool:
            kwargs["tool_choice"] = {"type": "tool", "name": likely_tool}
            print(f"[Turn 0] Forcing {likely_tool} — skipping planning turn")
        elif turn > 0:
            # After turn 0, allow any tool but require at least one
            kwargs["tool_choice"] = {"type": "auto"}

        response = client.messages.create(**kwargs)
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        if response.stop_reason == "tool_use":
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    print(f"[Turn {turn}] Called: {block.name}")
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"[{block.name} completed]",
                    })
            messages.append({"role": "user", "content": results})

    return "Max turns reached"


# "Search" triggers forced web_search on turn 0 — no planning turn
result = run_agent_first_turn_forced("Search for the latest Python 3.13 release notes")
print(result)
```

**Expected Token Savings:** Forcing the right tool on turn 0 eliminates the entire planning turn (~500 tokens); for tasks where the tool is obvious, this is a pure saving with no quality loss.
**Environment:** Single-purpose agents with predictable tool use patterns; keyword-to-tool mapping is brittle for complex tasks but extremely effective for simple, well-scoped requests.

---

### Option 4 — Planning token cap with forced transition

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

TOOLS = [
    {
        "name": "take_action",
        "description": "Execute a specific action toward completing the task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action_description": {"type": "string"},
                "expected_outcome": {"type": "string"},
            },
            "required": ["action_description", "expected_outcome"],
        },
    },
]


def run_agent_with_token_cap(user_message: str, planning_token_budget: int = 300) -> str:
    """
    Allow planning up to a token budget, then force action.
    Uses max_tokens constraint on the first turn to limit planning verbosity.
    """
    messages = [{"role": "user", "content": user_message}]
    total_planning_tokens = 0

    for turn in range(10):
        # If we've spent too many tokens on planning, force a tool call
        over_budget = total_planning_tokens > planning_token_budget
        kwargs = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 512 if not over_budget else 1024,
            "tools": TOOLS,
            "messages": messages,
        }

        if over_budget:
            kwargs["tool_choice"] = {"type": "any"}
            if turn > 0 and messages[-1]["role"] != "user":
                messages.append({
                    "role": "user",
                    "content": (
                        f"You have spent {total_planning_tokens} tokens on planning. "
                        "Execute take_action immediately with the first concrete step."
                    ),
                })
            print(f"[Turn {turn}] Planning budget exceeded ({total_planning_tokens} tok) — forcing action")
        elif turn == 0:
            # First turn: allow brief planning but cap it at the budget
            kwargs["max_tokens"] = planning_token_budget
            print(f"[Turn {turn}] Planning turn (budget: {planning_token_budget} tokens)")

        response = client.messages.create(**kwargs)
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            text = next((b.text for b in response.content if hasattr(b, "text")), "")
            turn_tokens = response.usage.output_tokens
            total_planning_tokens += turn_tokens
            print(f"[Turn {turn}] Planning tokens this turn: {turn_tokens}, total: {total_planning_tokens}")

        elif response.stop_reason == "tool_use":
            total_planning_tokens = 0   # reset on action
            print(f"[Turn {turn}] Action taken — planning budget reset")
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"Action '{block.input.get('action_description')}' completed.",
                    })
                    if not results[-1]["content"]:
                        return "Task complete"
            messages.append({"role": "user", "content": results})

            # Check if we're done
            if turn >= 8:
                return "Task completed after many steps"

    return "Reached turn limit"


result = run_agent_with_token_cap(
    "Analyze the repo structure and refactor the authentication module",
    planning_token_budget=400,
)
print(result)
```

**Expected Token Savings:** Planning token cap directly limits the cost of planning turns; for an agent that would naturally produce 800-token plans for 5 turns (4000 tokens), capping at 300 saves ~2500 tokens before the forced action kicks in.
**Environment:** Agents on a strict token budget per task; the token cap is self-reinforcing — the more planning the model tries to do, the faster it hits the cap and is forced to act.

---

### Option 5 — State machine: track plan/execute/verify states

```python
import anthropic
from enum import Enum

client = anthropic.Anthropic(api_key="sk-live-...")


class AgentState(Enum):
    PLANNING = "planning"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMPLETE = "complete"


TOOLS = [
    {
        "name": "execute_step",
        "description": "Execute one concrete step of the plan.",
        "input_schema": {
            "type": "object",
            "properties": {
                "step_number": {"type": "integer"},
                "description": {"type": "string"},
            },
            "required": ["step_number", "description"],
        },
    },
    {
        "name": "mark_complete",
        "description": "Mark the task as complete with a summary.",
        "input_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    },
]

STATE_SYSTEMS = {
    AgentState.PLANNING: (
        "You are in PLANNING state. Produce ONE brief plan (max 3 steps). "
        "Then immediately call execute_step for step 1. Do not plan more than once."
    ),
    AgentState.EXECUTING: (
        "You are in EXECUTING state. You must call execute_step or mark_complete. "
        "Do not produce any text without also calling a tool."
    ),
    AgentState.VERIFYING: (
        "You are in VERIFYING state. Check if the task is complete. "
        "Call mark_complete if done, or execute_step if a step was missed."
    ),
}


def run_agent_state_machine(user_message: str) -> str:
    state = AgentState.PLANNING
    messages = [{"role": "user", "content": user_message}]
    steps_executed = 0
    MAX_PLANNING_TURNS = 1

    planning_turns = 0

    for turn in range(20):
        system = STATE_SYSTEMS.get(state, "You are a helpful assistant.")
        kwargs = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 512,
            "system": system,
            "tools": TOOLS,
            "messages": messages,
        }

        # Force tool use in EXECUTING and VERIFYING states
        if state in (AgentState.EXECUTING, AgentState.VERIFYING):
            kwargs["tool_choice"] = {"type": "any"}

        print(f"[Turn {turn}] State: {state.value}")
        response = client.messages.create(**kwargs)
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            if state == AgentState.PLANNING:
                planning_turns += 1
                if planning_turns >= MAX_PLANNING_TURNS:
                    state = AgentState.EXECUTING
                    print(f"[State] PLANNING → EXECUTING (max planning turns reached)")

        elif response.stop_reason == "tool_use":
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    if block.name == "execute_step":
                        steps_executed += 1
                        state = AgentState.EXECUTING
                        results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"Step {block.input['step_number']} done.",
                        })
                        if steps_executed >= 3:
                            state = AgentState.VERIFYING
                            print("[State] EXECUTING → VERIFYING")
                    elif block.name == "mark_complete":
                        state = AgentState.COMPLETE
                        return f"Complete: {block.input['summary']}"
                        results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": "Task marked complete.",
                        })
            messages.append({"role": "user", "content": results})

        if state == AgentState.COMPLETE:
            return "Task completed"

    return f"Max turns reached. Steps executed: {steps_executed}"


result = run_agent_state_machine(
    "Create a Python script that fetches weather data and sends a daily email summary"
)
print(result)
```

**Expected Token Savings:** State machine enforces at most 1 planning turn before the agent is locked into EXECUTING state; prevents the open-ended planning loop entirely by making the transition to execution a one-way door.
**Environment:** Complex multi-step tasks with clear phase boundaries (plan → execute → verify); the state machine is more robust than heuristic detection for long-horizon agents.

---

### Option 6 — Self-monitoring: agent detects its own planning loop

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

TOOLS = [
    {
        "name": "take_concrete_action",
        "description": "Take a concrete, observable action toward the goal.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "justification": {"type": "string"},
            },
            "required": ["action", "justification"],
        },
    },
]

SELF_MONITOR_SYSTEM = """
You are a task execution agent with a critical meta-rule:

ANTI-LOOP RULE: Before writing your response, ask yourself:
"Am I about to describe what I will do, or am I about to do it?"

If you are about to describe what you will do → STOP. Call take_concrete_action instead.
If you are about to do it (by calling a tool) → proceed.

Signs you are in a planning loop:
- Your response starts with "Let me...", "I will...", "First, I should..."
- Your response lists numbered steps without tool calls
- You are writing the same plan you wrote in the previous turn
- You are questioning whether your plan is correct instead of testing it

When in doubt: act, observe, adjust. Do not plan for more than one turn.
"""


def run_agent_self_monitoring(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    last_response_text = ""

    for turn in range(10):
        # After 2 turns, force tool use regardless
        force_tool = turn >= 2
        kwargs = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 1024,
            "system": SELF_MONITOR_SYSTEM,
            "tools": TOOLS,
            "messages": messages,
        }
        if force_tool:
            kwargs["tool_choice"] = {"type": "any"}

        response = client.messages.create(**kwargs)
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            text = next((b.text for b in response.content if hasattr(b, "text")), "")
            # Detect repeated content (same plan restated)
            if text and text[:100] == last_response_text[:100] and len(text) > 50:
                print(f"[Self-monitor] Repeated planning detected — forcing tool on next turn")
            last_response_text = text
            if turn >= 2:
                return text

        elif response.stop_reason == "tool_use":
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    print(f"[Action] {block.input.get('action', '?')}")
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"Action completed: {block.input.get('action')}",
                    })
            messages.append({"role": "user", "content": results})

    # Comparison table
    # | Option | Loop Detection | Intervention | Aggressiveness |
    # |--------|---------------|-------------|----------------|
    # | 1 Planning counter | planning_turns count | tool_choice: any | Medium |
    # | 2 Progress tracking | zero-progress turns | Abort after 3 | High |
    # | 3 First-turn forced | keyword match | tool_choice: tool | High (turn 0) |
    # | 4 Token cap | output token count | tool_choice: any | Budget-based |
    # | 5 State machine | state transitions | One-way EXECUTING lock | Structural |
    # | 6 Self-monitoring | system prompt rule | Hard limit at turn 2 | Hybrid |

    return "Max turns reached"


result = run_agent_self_monitoring(
    "Set up a CI/CD pipeline for the project and write documentation"
)
print(result)
```

**Expected Token Savings:** Self-monitoring system prompt reduces planning verbosity by instructing the model to notice its own loop pattern; the hard turn-2 tool requirement caps worst-case planning cost at 2 × ~500 tokens = 1000 tokens before forced execution.
**Environment:** General-purpose agents; the self-monitoring system prompt is the most behaviorally aligned intervention — it trains the model to recognize and self-correct the loop rather than relying purely on external enforcement.
