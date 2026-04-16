---
title: "Agent Doesn't Implement Dead-End Detection for Unsolvable Tasks"
description: "Detect when a task is genuinely unsolvable—due to contradictory constraints, missing prerequisites, or tool limitations—and fail gracefully with a clear explanation instead of looping indefinitely."
difficulty: intermediate
category: loop-stuck
tags: [loop-stuck, dead-end, unsolvable, failure-modes, graceful-degradation]
---

## Problem

Agents encounter tasks they cannot complete: contradictory requirements, missing tools, insufficient permissions, or impossible constraints. Instead of detecting and reporting these dead ends, they loop—trying the same failing approaches repeatedly until they hit token limits or timeouts, wasting resources and providing no useful feedback. Dead-end detection terminates gracefully and tells users exactly why the task failed.

## Solutions

### Option 1: Repeated-Action Detector

Track recent actions and abort if the same approach is attempted multiple times without progress.

```python
import asyncio
import hashlib
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

client = AsyncAnthropic()

@dataclass
class ActionMemory:
    max_repeat_threshold: int = 3
    window_size: int = 10
    _history: list[str] = field(default_factory=list)

    def record(self, action_description: str) -> bool:
        """Returns True if this is a dead-end (repeated action detected)."""
        action_hash = hashlib.md5(action_description.encode()).hexdigest()
        self._history.append(action_hash)

        # Check within recent window
        recent = self._history[-self.window_size:]
        repeat_count = recent.count(action_hash)

        if repeat_count >= self.max_repeat_threshold:
            return True  # Dead end detected
        return False

    def unique_actions_count(self) -> int:
        return len(set(self._history))

    def total_actions(self) -> int:
        return len(self._history)

class DeadEndDetectingAgent:
    MAX_TURNS = 10

    def __init__(self):
        self.memory = ActionMemory(max_repeat_threshold=3)

    async def run_task(self, task: str) -> dict:
        messages = [{"role": "user", "content": task}]
        tools = [{
            "name": "search",
            "description": "Search for information",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"]
            }
        }]

        for turn in range(self.MAX_TURNS):
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                tools=tools,
                messages=messages,
            )

            if response.stop_reason == "end_turn":
                final_text = next(
                    (b.text for b in response.content if hasattr(b, "text")), ""
                )
                return {"status": "complete", "answer": final_text, "turns": turn + 1}

            if response.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": response.content})
                tool_results = []

                for block in response.content:
                    if block.type == "tool_use":
                        # Record the action
                        action_desc = f"{block.name}:{block.input}"
                        is_dead_end = self.memory.record(action_desc)

                        if is_dead_end:
                            return {
                                "status": "dead_end",
                                "reason": (
                                    f"Repeated action detected: attempted '{block.name}' "
                                    f"with similar inputs {self.memory.max_repeat_threshold}+ times. "
                                    f"Task may be unsolvable with available tools."
                                ),
                                "unique_approaches_tried": self.memory.unique_actions_count(),
                                "total_attempts": self.memory.total_actions(),
                                "turns": turn + 1,
                            }

                        # Simulate tool returning no useful results
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": "No results found for this query."
                        })

                messages.append({"role": "user", "content": tool_results})

        return {
            "status": "timeout",
            "reason": f"Exceeded {self.MAX_TURNS} turns without completion",
            "turns": self.MAX_TURNS,
        }

async def demo_repeated_action_detection():
    agent = DeadEndDetectingAgent()

    # Task that will loop: agent can't find info that doesn't exist
    result = await agent.run_task(
        "Find the exact population of the fictional city 'Zantoria' using the search tool."
    )

    print(f"Status: {result['status']}")
    print(f"Reason: {result.get('reason', result.get('answer', ''))}")
    if "unique_approaches_tried" in result:
        print(f"Unique approaches tried: {result['unique_approaches_tried']}")

asyncio.run(demo_repeated_action_detection())
```

### Option 2: Constraint Contradiction Detector

Use the model itself to check whether the task contains contradictory or impossible requirements before attempting it.

```python
import asyncio
import json
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

FEASIBILITY_SYSTEM = """You are a task feasibility analyzer. Before executing any task, determine if it is solvable.

Analyze the task for:
1. Contradictory requirements (e.g., "be brief but exhaustive")
2. Missing prerequisites (e.g., requires access you don't have)
3. Logical impossibilities (e.g., "find a prime number divisible by 6")
4. Scope impossibilities (e.g., requires real-time data you can't access)
5. Circular dependencies (e.g., "A requires B which requires A")

Return JSON:
{
  "feasible": true/false,
  "confidence": 0.0-1.0,
  "issues": ["issue1", "issue2"],
  "recommendation": "proceed|clarify|refuse",
  "reason": "brief explanation"
}
Respond ONLY with the JSON."""

async def check_feasibility(task: str) -> dict:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=FEASIBILITY_SYSTEM,
        messages=[{"role": "user", "content": f"Task: {task}"}]
    )
    try:
        return json.loads(response.content[0].text)
    except json.JSONDecodeError:
        return {"feasible": True, "confidence": 0.5, "issues": [], "recommendation": "proceed"}

async def execute_with_feasibility_check(task: str) -> dict:
    """Run feasibility check before attempting task."""
    feasibility = await check_feasibility(task)

    if not feasibility.get("feasible", True) and feasibility.get("confidence", 0) > 0.75:
        return {
            "status": "infeasible",
            "issues": feasibility.get("issues", []),
            "reason": feasibility.get("reason", "Task has contradictory or impossible requirements"),
            "recommendation": feasibility.get("recommendation"),
        }

    if feasibility.get("recommendation") == "clarify":
        return {
            "status": "needs_clarification",
            "issues": feasibility.get("issues", []),
            "reason": feasibility.get("reason", "Task requires clarification before proceeding"),
        }

    # Proceed with execution
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": task}]
    )
    return {
        "status": "complete",
        "answer": response.content[0].text,
        "feasibility_check": feasibility,
    }

async def demo_constraint_detection():
    test_tasks = [
        "Write a one-word essay that is also 10 pages long.",
        "Find a number that is both even and odd.",
        "Summarize the article at this URL: [url not provided].",
        "List the top 5 Python best practices.",  # Feasible
        "Sort this list in both ascending and descending order simultaneously.",
    ]

    for task in test_tasks:
        result = await execute_with_feasibility_check(task)
        status = result["status"]
        print(f"\n[{status.upper()}] {task[:60]}")
        if status == "infeasible":
            print(f"  Issues: {result['issues']}")
            print(f"  Reason: {result['reason']}")
        elif status == "complete":
            print(f"  Answer: {result['answer'].strip()[:100]}...")

asyncio.run(demo_constraint_detection())
```

### Option 3: Progress-Based Dead-End Detection

Measure whether the agent is making progress toward the goal; abort if progress stalls.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

client = AsyncAnthropic()

@dataclass
class ProgressTracker:
    goal: str
    stall_threshold: int = 3      # Turns without progress before aborting
    min_progress_score: float = 0.1  # Minimum progress per turn

    _turn_scores: list[float] = field(default_factory=list)
    _stall_count: int = 0

    def record_turn(self, response_text: str) -> bool:
        """
        Estimate progress as fraction of goal-related keywords present.
        Returns True if dead end detected (progress stalled).
        """
        goal_words = set(self.goal.lower().split()) - {"the", "a", "an", "is", "to", "of"}
        response_words = set(response_text.lower().split())
        progress = len(goal_words & response_words) / max(len(goal_words), 1)

        # Detect stall: progress not improving
        if self._turn_scores:
            delta = progress - self._turn_scores[-1]
            if delta < self.min_progress_score:
                self._stall_count += 1
            else:
                self._stall_count = 0
        self._turn_scores.append(progress)

        return self._stall_count >= self.stall_threshold

    def summary(self) -> dict:
        return {
            "total_turns": len(self._turn_scores),
            "stall_count": self._stall_count,
            "progress_trajectory": [f"{s:.2f}" for s in self._turn_scores],
            "final_progress": self._turn_scores[-1] if self._turn_scores else 0,
        }

class ProgressAwareAgent:
    def __init__(self, goal: str):
        self.goal = goal
        self.tracker = ProgressTracker(goal=goal)

    async def work_toward_goal(self, max_turns: int = 8) -> dict:
        messages = [{"role": "user", "content": f"Goal: {self.goal}"}]

        for turn in range(max_turns):
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                system=(
                    "Work toward the given goal step by step. "
                    "If you cannot make progress, explicitly state why."
                ),
                messages=messages,
            )
            text = response.content[0].text
            messages.append({"role": "assistant", "content": text})

            is_dead_end = self.tracker.record_turn(text)
            if is_dead_end:
                return {
                    "status": "dead_end",
                    "reason": f"Progress stalled for {self.tracker.stall_threshold} consecutive turns",
                    "last_response": text[:150],
                    "progress_summary": self.tracker.summary(),
                }

            if response.stop_reason == "end_turn" and turn > 0:
                return {
                    "status": "complete",
                    "answer": text,
                    "progress_summary": self.tracker.summary(),
                }

            # Ask for next step
            messages.append({
                "role": "user",
                "content": "Continue toward the goal. What's the next step?"
            })

        return {
            "status": "timeout",
            "reason": f"Exceeded {max_turns} turns",
            "progress_summary": self.tracker.summary(),
        }

async def demo_progress_tracking():
    agent = ProgressAwareAgent(
        goal="Find the email address of the CEO of a company that doesn't exist yet"
    )
    result = await agent.work_toward_goal()

    print(f"Status: {result['status']}")
    print(f"Reason: {result.get('reason', '')}")
    print(f"Progress trajectory: {result['progress_summary']['progress_trajectory']}")

asyncio.run(demo_progress_tracking())
```

### Option 4: Tool-Capability Gap Analyzer

Before starting, check whether the agent's tools are sufficient to complete the task.

```python
import asyncio
import json
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

@dataclass
class ToolCapabilityAnalysis:
    tools_available: list[str]
    tools_required: list[str]
    missing_tools: list[str]
    can_complete: bool
    confidence: float
    workaround: str | None

TOOL_GAP_SYSTEM = """You are a tool capability analyzer for AI agents.

Given a task and a list of available tools, determine if the task can be completed.

Analyze:
1. What tools are REQUIRED to complete this task?
2. Which required tools are MISSING from the available set?
3. Can the task be completed without the missing tools? If so, how?

Return JSON:
{
  "tools_required": ["tool1", "tool2"],
  "missing_tools": ["tool3"],
  "can_complete": true/false,
  "confidence": 0.0-1.0,
  "workaround": "description or null",
  "blocker": "explanation if cannot complete"
}
Respond ONLY with JSON."""

async def analyze_tool_gap(task: str, available_tools: list[str]) -> ToolCapabilityAnalysis:
    prompt = (
        f"Task: {task}\n\n"
        f"Available tools: {', '.join(available_tools) if available_tools else 'none'}"
    )

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=TOOL_GAP_SYSTEM,
        messages=[{"role": "user", "content": prompt}]
    )

    try:
        data = json.loads(response.content[0].text)
    except json.JSONDecodeError:
        data = {}

    available_set = set(available_tools)
    required = data.get("tools_required", [])
    missing = data.get("missing_tools", [m for m in required if m not in available_set])

    return ToolCapabilityAnalysis(
        tools_available=available_tools,
        tools_required=required,
        missing_tools=missing,
        can_complete=data.get("can_complete", True),
        confidence=data.get("confidence", 0.5),
        workaround=data.get("workaround"),
    )

async def execute_with_capability_check(
    task: str, available_tools: list[str]
) -> dict:
    analysis = await analyze_tool_gap(task, available_tools)

    if not analysis.can_complete and analysis.confidence > 0.7:
        return {
            "status": "cannot_complete",
            "missing_tools": analysis.missing_tools,
            "tools_required": analysis.tools_required,
            "workaround": analysis.workaround,
            "message": (
                f"Task requires: {', '.join(analysis.tools_required)}. "
                f"Missing: {', '.join(analysis.missing_tools)}."
            ),
        }

    if analysis.workaround:
        print(f"[CapabilityCheck] Missing tools detected, proceeding with workaround: "
              f"{analysis.workaround[:80]}")

    # Proceed with available tools
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": task}]
    )
    return {
        "status": "complete",
        "answer": response.content[0].text,
        "tools_used": analysis.tools_required,
    }

async def demo_tool_gap():
    test_cases = [
        (
            "Send an email to john@example.com summarizing this conversation.",
            ["search", "calculator"],
        ),
        (
            "Look up the weather in Tokyo.",
            ["weather_api", "search"],
        ),
        (
            "What is 2 + 2?",
            ["calculator"],
        ),
        (
            "Query our PostgreSQL database for user counts.",
            ["search"],  # Missing database tool
        ),
    ]

    for task, tools in test_cases:
        result = await execute_with_capability_check(task, tools)
        print(f"\n[{result['status'].upper()}] {task[:60]}")
        if result["status"] == "cannot_complete":
            print(f"  Missing: {result['missing_tools']}")
            print(f"  Workaround: {result.get('workaround', 'none')}")
        else:
            print(f"  Answer: {result['answer'].strip()[:80]}...")

asyncio.run(demo_tool_gap())
```

### Option 5: Hierarchical Dead-End Classification

Classify the type of dead end to give users actionable next steps, not just a failure message.

```python
import asyncio
import json
from anthropic import AsyncAnthropic
from enum import Enum

client = AsyncAnthropic()

class DeadEndType(Enum):
    MISSING_INFO = "missing_information"
    PERMISSION_DENIED = "permission_denied"
    LOGICAL_CONTRADICTION = "logical_contradiction"
    TOOL_LIMITATION = "tool_limitation"
    SCOPE_EXCEEDED = "scope_exceeded"
    AMBIGUOUS_REQUIREMENTS = "ambiguous_requirements"
    UNKNOWN = "unknown"

DEAD_END_CLASSIFIER = """Classify why this AI agent task reached a dead end.

Dead-end types:
- "missing_information": Required data/context was not provided
- "permission_denied": Would require access the agent doesn't have
- "logical_contradiction": Requirements contradict each other
- "tool_limitation": Agent's tools cannot perform what's needed
- "scope_exceeded": Task exceeds agent's knowledge or capability domain
- "ambiguous_requirements": Task is too vague to execute
- "unknown": Cannot classify

Return JSON:
{
  "dead_end_type": "...",
  "confidence": 0.0-1.0,
  "user_action_required": "what the user should do to resolve this",
  "is_recoverable": true/false,
  "summary": "one sentence explanation"
}
Respond ONLY with JSON."""

async def classify_dead_end(task: str, failure_description: str) -> dict:
    prompt = f"Task: {task}\nFailure: {failure_description}"
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=DEAD_END_CLASSIFIER,
        messages=[{"role": "user", "content": prompt}]
    )
    try:
        return json.loads(response.content[0].text)
    except json.JSONDecodeError:
        return {
            "dead_end_type": "unknown",
            "confidence": 0.5,
            "is_recoverable": False,
            "user_action_required": "Rephrase the task or provide more context.",
            "summary": "Task failed for unknown reasons."
        }

async def run_task_with_classification(task: str) -> dict:
    """Run a task, and if it fails, classify the dead end."""
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=(
                "Complete the task if possible. "
                "If you cannot complete it, respond with 'CANNOT_COMPLETE: [reason]' "
                "on the first line."
            ),
            messages=[{"role": "user", "content": task}]
        )
        text = response.content[0].text

        if text.startswith("CANNOT_COMPLETE:"):
            failure_reason = text.split(":", 1)[1].strip()
            classification = await classify_dead_end(task, failure_reason)
            return {
                "status": "dead_end",
                "dead_end_type": classification["dead_end_type"],
                "is_recoverable": classification["is_recoverable"],
                "user_action": classification["user_action_required"],
                "summary": classification["summary"],
            }

        return {"status": "complete", "answer": text}

    except Exception as e:
        classification = await classify_dead_end(task, str(e))
        return {
            "status": "dead_end",
            "dead_end_type": classification["dead_end_type"],
            "is_recoverable": classification["is_recoverable"],
            "user_action": classification["user_action_required"],
            "summary": classification["summary"],
        }

async def demo_classification():
    dead_end_tasks = [
        "Access the user's private messages from last week.",
        "Create a report for Q4 2024 using data from our ERP system.",
        "Find a word that means both 'fast' and 'slow' simultaneously.",
        "Explain how our proprietary ML model works internally.",
    ]

    for task in dead_end_tasks:
        result = await run_task_with_classification(task)
        print(f"\nTask: {task[:55]}")
        print(f"  Status: {result['status']}")
        if result['status'] == 'dead_end':
            print(f"  Type: {result.get('dead_end_type', 'unknown')}")
            print(f"  Recoverable: {result.get('is_recoverable', False)}")
            print(f"  Action needed: {result.get('user_action', '')[:100]}")
        else:
            print(f"  Answer: {result['answer'].strip()[:80]}")

asyncio.run(demo_classification())
```

### Option 6: Collaborative Dead-End Resolution

When a dead end is detected, automatically draft a clarification request to help the user resolve it.

```python
import asyncio
import json
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

@dataclass
class DeadEndResolution:
    dead_end_summary: str
    clarifying_questions: list[str]
    alternative_approaches: list[str]
    minimum_info_needed: str
    estimated_resolvable: bool

RESOLUTION_SYSTEM = """You help users resolve AI agent dead ends collaboratively.

When given a task and why it failed, generate:
1. A clear summary of the dead end (what's blocking progress)
2. Specific clarifying questions that would unblock the task
3. Alternative approaches the user might not have considered
4. The minimum additional information needed to proceed

Return JSON:
{
  "dead_end_summary": "...",
  "clarifying_questions": ["q1", "q2", "q3"],
  "alternative_approaches": ["approach1", "approach2"],
  "minimum_info_needed": "...",
  "estimated_resolvable": true/false
}
Respond ONLY with JSON."""

async def generate_resolution(task: str, failure_reason: str) -> DeadEndResolution:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=RESOLUTION_SYSTEM,
        messages=[{"role": "user", "content": f"Task: {task}\nFailed because: {failure_reason}"}]
    )
    try:
        data = json.loads(response.content[0].text)
    except json.JSONDecodeError:
        data = {}

    return DeadEndResolution(
        dead_end_summary=data.get("dead_end_summary", failure_reason),
        clarifying_questions=data.get("clarifying_questions", []),
        alternative_approaches=data.get("alternative_approaches", []),
        minimum_info_needed=data.get("minimum_info_needed", "More context needed."),
        estimated_resolvable=data.get("estimated_resolvable", False),
    )

class CollaborativeAgent:
    async def attempt(self, task: str) -> dict:
        """Attempt task; if dead end, generate collaborative resolution."""
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=(
                "Attempt the task. If it is impossible or missing critical information, "
                "respond with exactly: DEAD_END: <brief reason>"
            ),
            messages=[{"role": "user", "content": task}]
        )
        text = response.content[0].text.strip()

        if text.startswith("DEAD_END:"):
            reason = text[9:].strip()
            resolution = await generate_resolution(task, reason)

            return {
                "status": "dead_end",
                "summary": resolution.dead_end_summary,
                "clarifying_questions": resolution.clarifying_questions,
                "alternatives": resolution.alternative_approaches,
                "minimum_needed": resolution.minimum_info_needed,
                "resolvable": resolution.estimated_resolvable,
            }

        return {"status": "complete", "answer": text}

async def demo_collaborative_resolution():
    agent = CollaborativeAgent()

    stuck_tasks = [
        "Generate a report based on our sales data from last quarter.",
        "Write a function that reads from our internal database schema.",
        "Find the contact info for the project manager assigned to this ticket.",
    ]

    for task in stuck_tasks:
        result = await agent.attempt(task)
        print(f"\nTask: {task[:60]}")
        print(f"Status: {result['status']}")

        if result["status"] == "dead_end":
            print(f"Summary: {result['summary']}")
            print(f"Minimum needed: {result['minimum_needed']}")
            if result["clarifying_questions"]:
                print("Clarifying questions:")
                for q in result["clarifying_questions"][:3]:
                    print(f"  • {q}")
            if result["alternatives"]:
                print("Alternatives:")
                for a in result["alternatives"][:2]:
                    print(f"  → {a}")

asyncio.run(demo_collaborative_resolution())
```

## Comparison

| Approach | Detection Method | Actionability | Overhead | Best For |
|---|---|---|---|---|
| Repeated-Action Detector | Action hashing | Low (reports loop) | Minimal | Infinite loop prevention |
| Constraint Contradiction | Pre-flight LLM check | High (explains why) | 1 LLM call | Impossible requirements |
| Progress Tracking | Score delta per turn | Medium (stall detection) | None | Multi-step goal pursuit |
| Tool-Capability Gap | Pre-flight LLM check | High (lists missing tools) | 1 LLM call | Tool-dependent tasks |
| Dead-End Classification | Post-failure LLM | Very High (type + action) | 1 LLM call | User-facing products |
| Collaborative Resolution | Post-failure LLM | Highest (questions + alts) | 1-2 LLM calls | Complex collaborative tasks |

**Choose Repeated-Action Detector** as a safety net in every agent loop—it's zero-overhead and prevents infinite loops universally. **Choose Constraint Contradiction Detection** as a pre-flight check for tasks that arrive from users and may contain impossible requirements. **Choose Collaborative Resolution** for user-facing products where helping the user reformulate the task is more valuable than simply reporting failure.
