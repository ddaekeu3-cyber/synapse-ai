---
layout: solution
title: "Agent Doesn't Implement Goal Anchoring to Prevent Drift"
category: loop-stuck
description: "Long-running agents gradually drift from their original objective during multi-step tasks. Without goal anchoring, intermediate sub-tasks hijack focus and the agent never completes the primary goal."
tags: [loop-stuck, goal-drift, anchoring, long-tasks, state-management, multi-step]
---

# Agent Doesn't Implement Goal Anchoring to Prevent Drift

## Problem

In long agentic workflows, the model loses track of the original goal after processing many tool results and intermediate reasoning steps. It begins optimizing for sub-goals, gets distracted by tangential information, or simply forgets what it was originally asked to accomplish. This manifests as tasks that run for many steps but never return the primary deliverable.

## Why This Happens

The model's attention mechanism weighs recent context more heavily than early context. A goal stated at turn 1 competes with dozens of tool results and assistant messages by turn 20. Without explicit re-anchoring, the original intent fades and the agent enters a drift pattern — busy but not making progress toward the actual objective.

## Solutions

### Option 1: Static Goal Injection — Re-inject goal in every system prompt turn

```python
import anthropic
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class AnchoredAgent:
    goal: str
    client: anthropic.Anthropic = field(default_factory=anthropic.Anthropic)
    history: list = field(default_factory=list)
    step: int = 0
    max_steps: int = 30

    def build_system_prompt(self) -> str:
        return f"""You are a goal-directed agent.

PRIMARY GOAL (never lose sight of this):
{self.goal}

Current step: {self.step}/{self.max_steps}

Instructions:
- Every action must directly contribute to the PRIMARY GOAL above.
- If a sub-task would not advance the PRIMARY GOAL, skip it.
- When the PRIMARY GOAL is complete, respond with GOAL_COMPLETE followed by your final answer.
- If you cannot make progress, respond with GOAL_BLOCKED and explain why."""

    def run(self, user_message: str) -> str:
        self.history.append({"role": "user", "content": user_message})

        while self.step < self.max_steps:
            self.step += 1

            response = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                system=self.build_system_prompt(),  # Re-injected every turn
                messages=self.history,
            )

            assistant_text = response.content[0].text
            self.history.append({"role": "assistant", "content": assistant_text})

            if "GOAL_COMPLETE" in assistant_text:
                return assistant_text.split("GOAL_COMPLETE")[-1].strip()
            if "GOAL_BLOCKED" in assistant_text:
                return f"Agent blocked: {assistant_text}"

            # Continue conversation
            self.history.append({
                "role": "user",
                "content": "Continue working toward the PRIMARY GOAL."
            })

        return "Max steps reached without completing goal."


# Usage
agent = AnchoredAgent(goal="Write a complete REST API for a todo list with CRUD endpoints")
result = agent.run("Begin implementing the API.")
print(result)

# Expected Token Savings: ~5-15% overhead for goal re-injection; saves 30-60% by preventing wasted drift steps
# Environment: Production agents with tasks > 10 steps; any long-running autonomous workflow
```

### Option 2: Goal Milestone Tracker — Break goal into checkpoints and verify progress

```python
import anthropic
import json
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class Milestone:
    id: str
    description: str
    completed: bool = False
    evidence: str = ""

class MilestoneAnchoredAgent:
    def __init__(self, goal: str, milestones: list[str]):
        self.client = anthropic.Anthropic()
        self.goal = goal
        self.milestones = [
            Milestone(id=f"M{i+1}", description=m)
            for i, m in enumerate(milestones)
        ]
        self.history: list[dict] = []

    def milestone_status(self) -> str:
        lines = [f"GOAL: {self.goal}", "", "MILESTONES:"]
        for m in self.milestones:
            status = "✓" if m.completed else "○"
            lines.append(f"  [{status}] {m.id}: {m.description}")
            if m.completed and m.evidence:
                lines.append(f"       Evidence: {m.evidence[:80]}")
        pending = [m for m in self.milestones if not m.completed]
        if pending:
            lines.append(f"\nNEXT TARGET: {pending[0].id} — {pending[0].description}")
        return "\n".join(lines)

    def check_milestone_completion(self, response_text: str) -> None:
        """Ask model to self-report which milestones were completed."""
        check_response = self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system="You extract milestone completion status from agent responses. Return JSON only.",
            messages=[{
                "role": "user",
                "content": f"""Agent response:
{response_text}

Pending milestones:
{json.dumps([{"id": m.id, "description": m.description} for m in self.milestones if not m.completed])}

Which milestones (if any) were completed in this response?
Return: {{"completed": [{{"id": "M1", "evidence": "brief quote"}}]}}"""
            }]
        )
        try:
            data = json.loads(check_response.content[0].text)
            milestone_map = {m.id: m for m in self.milestones}
            for item in data.get("completed", []):
                if item["id"] in milestone_map:
                    milestone_map[item["id"]].completed = True
                    milestone_map[item["id"]].evidence = item.get("evidence", "")
        except (json.JSONDecodeError, KeyError):
            pass

    def run(self, turns: int = 20) -> str:
        self.history.append({
            "role": "user",
            "content": f"Begin working on this goal:\n{self.goal}"
        })

        for _ in range(turns):
            if all(m.completed for m in self.milestones):
                return f"All milestones complete!\n{self.milestone_status()}"

            response = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                system=f"""You are a milestone-driven agent.\n\n{self.milestone_status()}

Focus exclusively on completing the NEXT TARGET milestone.""",
                messages=self.history,
            )

            text = response.content[0].text
            self.history.append({"role": "assistant", "content": text})
            self.check_milestone_completion(text)

            pending = [m for m in self.milestones if not m.completed]
            if pending:
                self.history.append({
                    "role": "user",
                    "content": f"Continue. Next target: {pending[0].description}"
                })

        return f"Timeout.\n{self.milestone_status()}"


# Usage
agent = MilestoneAnchoredAgent(
    goal="Build a Python web scraper for product prices",
    milestones=[
        "Set up HTTP client with retry logic",
        "Parse product name and price from HTML",
        "Store results to CSV file",
        "Add rate limiting and error handling",
    ]
)
print(agent.run())

# Expected Token Savings: 20-40% reduction by eliminating off-track steps; milestone checker uses cheap Haiku model
# Environment: Complex multi-step tasks with clear deliverables; software development, research, data pipelines
```

### Option 3: Drift Detector — Automatically detect and correct goal drift

```python
import anthropic
import json
from dataclasses import dataclass, field

@dataclass
class DriftDetector:
    goal: str
    drift_threshold: float = 0.4  # 0=no drift, 1=completely off-track
    client: anthropic.Anthropic = field(default_factory=anthropic.Anthropic)

    def score_drift(self, recent_actions: list[str]) -> float:
        """Use Haiku to score how far recent actions deviate from goal."""
        if not recent_actions:
            return 0.0

        response = self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            system="You score goal drift. Return JSON only: {\"drift_score\": 0.0-1.0, \"reason\": \"...\"}",
            messages=[{
                "role": "user",
                "content": f"""GOAL: {self.goal}

RECENT ACTIONS:
{chr(10).join(f"- {a}" for a in recent_actions[-5:])}

Score drift: 0.0 = perfectly on track, 1.0 = completely off-track."""
            }]
        )
        try:
            data = json.loads(response.content[0].text)
            return float(data.get("drift_score", 0.0))
        except (json.JSONDecodeError, ValueError, KeyError):
            return 0.0


class DriftCorrectingAgent:
    def __init__(self, goal: str):
        self.client = anthropic.Anthropic()
        self.goal = goal
        self.detector = DriftDetector(goal=goal)
        self.history: list[dict] = []
        self.action_log: list[str] = []

    def correction_prompt(self, drift_score: float) -> str:
        severity = "CRITICAL" if drift_score > 0.7 else "WARNING"
        return (
            f"[{severity}: Goal drift detected — score {drift_score:.2f}]\n"
            f"You have drifted from your PRIMARY GOAL: {self.goal}\n"
            f"STOP current approach. Refocus entirely on the primary goal.\n"
            f"What is the single most important next action to achieve the goal?"
        )

    def run(self, initial_message: str, turns: int = 25) -> str:
        self.history.append({"role": "user", "content": initial_message})

        for turn in range(turns):
            response = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                system=f"PRIMARY GOAL: {self.goal}\n\nAlways work toward this goal.",
                messages=self.history,
            )

            text = response.content[0].text
            self.history.append({"role": "assistant", "content": text})
            self.action_log.append(text[:200])  # Log summary of action

            if "COMPLETE" in text.upper() and self.goal.lower()[:20] in text.lower():
                return text

            # Check for drift every 3 turns
            if turn > 0 and turn % 3 == 0:
                drift = self.detector.score_drift(self.action_log)
                if drift > self.detector.drift_threshold:
                    correction = self.correction_prompt(drift)
                    self.history.append({"role": "user", "content": correction})
                    continue

            self.history.append({
                "role": "user",
                "content": f"Continue toward: {self.goal}"
            })

        return f"Completed {turns} turns. Final state: {self.history[-2]['content'][:500]}"


# Usage
agent = DriftCorrectingAgent(goal="Analyze sales CSV and output top 10 products by revenue")
result = agent.run("Start by loading the sales data.")
print(result)

# Expected Token Savings: 25-45% by catching drift early; Haiku drift scorer costs ~50 tokens per check
# Environment: Research agents, data analysis tasks, agents prone to tangent exploration
```

### Option 4: Goal Stack — Push/pop sub-goals while preserving original

```python
import anthropic
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class GoalFrame:
    goal: str
    context: str
    depth: int

class GoalStackAgent:
    """Agent that maintains an explicit stack of goals to prevent drift."""

    MAX_DEPTH = 3  # Prevent infinite sub-goal nesting

    def __init__(self, primary_goal: str):
        self.client = anthropic.Anthropic()
        self.stack: list[GoalFrame] = [GoalFrame(goal=primary_goal, context="", depth=0)]
        self.history: list[dict] = []
        self.completed_subgoals: list[str] = []

    @property
    def current_goal(self) -> GoalFrame:
        return self.stack[-1]

    @property
    def primary_goal(self) -> str:
        return self.stack[0].goal

    def push_subgoal(self, subgoal: str, context: str = "") -> bool:
        if len(self.stack) >= self.MAX_DEPTH:
            return False  # Reject; too deep
        self.stack.append(GoalFrame(
            goal=subgoal,
            context=context,
            depth=len(self.stack)
        ))
        return True

    def pop_subgoal(self, result: str) -> Optional[GoalFrame]:
        if len(self.stack) <= 1:
            return None  # Never pop primary goal
        completed = self.stack.pop()
        self.completed_subgoals.append(f"{completed.goal}: {result[:100]}")
        return completed

    def system_prompt(self) -> str:
        stack_view = "\n".join(
            f"{'  ' * f.depth}[{'PRIMARY' if f.depth == 0 else f'SUB-{f.depth}'}] {f.goal}"
            for f in self.stack
        )
        completed_view = "\n".join(f"  ✓ {s}" for s in self.completed_subgoals[-5:])

        return f"""You are a goal-stack agent.

GOAL HIERARCHY:
{stack_view}

CURRENT FOCUS: {self.current_goal.goal}
PRIMARY GOAL (never abandon): {self.primary_goal}

COMPLETED SUB-GOALS:
{completed_view or '  (none yet)'}

Commands you may use in your response:
- PUSH_SUBGOAL: <subgoal description> — Start a sub-goal (max depth {self.MAX_DEPTH})
- POP_SUBGOAL: <result summary> — Complete current sub-goal and return to parent
- PRIMARY_COMPLETE: <final answer> — Declare the primary goal done"""

    def parse_command(self, text: str) -> tuple[str, str]:
        """Extract command and argument from response."""
        for cmd in ["PUSH_SUBGOAL", "POP_SUBGOAL", "PRIMARY_COMPLETE"]:
            if cmd + ":" in text:
                arg = text.split(cmd + ":")[1].split("\n")[0].strip()
                return cmd, arg
        return "", ""

    def run(self, turns: int = 30) -> str:
        self.history.append({
            "role": "user",
            "content": f"Begin working on: {self.primary_goal}"
        })

        for _ in range(turns):
            response = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                system=self.system_prompt(),
                messages=self.history,
            )
            text = response.content[0].text
            self.history.append({"role": "assistant", "content": text})

            cmd, arg = self.parse_command(text)

            if cmd == "PUSH_SUBGOAL":
                pushed = self.push_subgoal(arg)
                feedback = f"Sub-goal pushed: {arg}" if pushed else f"Sub-goal rejected (max depth {self.MAX_DEPTH})"
                self.history.append({"role": "user", "content": feedback})
            elif cmd == "POP_SUBGOAL":
                self.pop_subgoal(arg)
                self.history.append({
                    "role": "user",
                    "content": f"Sub-goal complete. Returning to: {self.current_goal.goal}"
                })
            elif cmd == "PRIMARY_COMPLETE":
                return f"Primary goal achieved: {arg}"
            else:
                self.history.append({
                    "role": "user",
                    "content": f"Continue toward current goal: {self.current_goal.goal}"
                })

        return f"Timeout. Stack depth: {len(self.stack)}. Primary: {self.primary_goal}"


# Usage
agent = GoalStackAgent("Build a Python CLI tool that converts Markdown to PDF")
result = agent.run()
print(result)

# Expected Token Savings: 10-20% by keeping sub-goals bounded; prevents runaway exploration
# Environment: Hierarchical tasks, software projects, research with well-defined sub-tasks
```

### Option 5: Periodic Goal Summarization — Re-derive goal from history every N turns

```python
import anthropic
from dataclasses import dataclass, field

@dataclass
class GoalSummarizingAgent:
    original_goal: str
    resummary_interval: int = 5  # Re-derive goal every N turns
    client: anthropic.Anthropic = field(default_factory=anthropic.Anthropic)
    history: list[dict] = field(default_factory=list)
    turn: int = 0
    derived_goal: str = ""

    def __post_init__(self):
        self.derived_goal = self.original_goal

    def rederive_goal(self) -> str:
        """Use Haiku to extract what goal is still being pursued from history."""
        if len(self.history) < 4:
            return self.original_goal

        recent = self.history[-10:]  # Last 10 messages
        response = self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system="You extract the current active goal from conversation history. Be concise.",
            messages=[{
                "role": "user",
                "content": f"""ORIGINAL GOAL: {self.original_goal}

RECENT CONVERSATION:
{chr(10).join(f"{m['role'].upper()}: {str(m['content'])[:200]}" for m in recent)}

In one sentence, what goal is the agent currently working toward?
If it matches the original goal, confirm it. If it has drifted, flag it and restate the original."""
            }]
        )
        return response.content[0].text.strip()

    def anchor_message(self) -> str:
        alignment = "ON TRACK" if self.original_goal.lower()[:30] in self.derived_goal.lower() else "CHECK ALIGNMENT"
        return (
            f"[Goal re-check at turn {self.turn} — {alignment}]\n"
            f"ORIGINAL: {self.original_goal}\n"
            f"CURRENT:  {self.derived_goal}\n"
            f"Continue pursuing the ORIGINAL goal."
        )

    def run(self, turns: int = 25) -> str:
        self.history.append({
            "role": "user",
            "content": f"Your goal: {self.original_goal}\n\nBegin working on it now."
        })

        for _ in range(turns):
            self.turn += 1

            # Periodically re-derive and re-anchor
            if self.turn % self.resummary_interval == 0:
                self.derived_goal = self.rederive_goal()
                anchor = self.anchor_message()
                self.history.append({"role": "user", "content": anchor})

            response = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                system=f"You are working toward: {self.original_goal}",
                messages=self.history,
            )

            text = response.content[0].text
            self.history.append({"role": "assistant", "content": text})

            if any(kw in text.upper() for kw in ["TASK COMPLETE", "DONE", "FINISHED", "GOAL ACHIEVED"]):
                return f"Completed at turn {self.turn}:\n{text}"

            self.history.append({
                "role": "user",
                "content": "Continue."
            })

        return f"Ran {turns} turns. Last output: {self.history[-2]['content'][:300]}"


# Usage
agent = GoalSummarizingAgent(
    original_goal="Create a Python script that monitors a folder and emails new files",
    resummary_interval=5
)
result = agent.run()
print(result)

# Expected Token Savings: 15-30% by identifying drift at turn 5, 10, 15 before it compounds
# Environment: Open-ended agents, research workflows, tasks longer than 10 turns
```

### Option 6: SQLite Goal Journal — Persist goal + progress log for drift forensics

```python
import anthropic
import sqlite3
import json
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path

class GoalJournalAgent:
    """Agent that persists goal progress to SQLite for drift detection and forensics."""

    DB_PATH = Path("/tmp/goal_journal.db")

    def __init__(self, goal: str, session_id: str = "default"):
        self.client = anthropic.Anthropic()
        self.goal = goal
        self.session_id = session_id
        self.history: list[dict] = []
        self._init_db()
        self._record_goal()

    def _init_db(self) -> None:
        with sqlite3.connect(self.DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS goal_sessions (
                    session_id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    status TEXT DEFAULT 'active'
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS goal_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn INTEGER NOT NULL,
                    action_summary TEXT NOT NULL,
                    goal_alignment_score REAL,
                    timestamp TEXT NOT NULL
                )
            """)

    def _record_goal(self) -> None:
        with sqlite3.connect(self.DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO goal_sessions VALUES (?, ?, ?, 'active')",
                (self.session_id, self.goal, datetime.utcnow().isoformat())
            )

    def _score_alignment(self, action_text: str) -> float:
        """Quick keyword overlap score: how much does this action relate to the goal?"""
        goal_words = set(self.goal.lower().split())
        action_words = set(action_text.lower().split())
        if not goal_words:
            return 1.0
        overlap = len(goal_words & action_words) / len(goal_words)
        return min(1.0, overlap * 3)  # Scale up since exact word match is rare

    def _record_turn(self, turn: int, action: str) -> float:
        score = self._score_alignment(action)
        with sqlite3.connect(self.DB_PATH) as conn:
            conn.execute(
                "INSERT INTO goal_turns VALUES (NULL, ?, ?, ?, ?, ?)",
                (self.session_id, turn, action[:500], score, datetime.utcnow().isoformat())
            )
        return score

    def _get_drift_trend(self) -> list[float]:
        with sqlite3.connect(self.DB_PATH) as conn:
            rows = conn.execute(
                "SELECT goal_alignment_score FROM goal_turns WHERE session_id = ? ORDER BY turn DESC LIMIT 5",
                (self.session_id,)
            ).fetchall()
        return [r[0] for r in rows]

    def _complete_session(self, status: str) -> None:
        with sqlite3.connect(self.DB_PATH) as conn:
            conn.execute(
                "UPDATE goal_sessions SET status = ? WHERE session_id = ?",
                (status, self.session_id)
            )

    def run(self, turns: int = 25) -> str:
        self.history.append({
            "role": "user",
            "content": f"Work toward this goal: {self.goal}"
        })

        for turn in range(1, turns + 1):
            response = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                system=(
                    f"PRIMARY GOAL: {self.goal}\n\n"
                    f"Session: {self.session_id}. Turn: {turn}/{turns}.\n"
                    f"Every action must advance the PRIMARY GOAL."
                ),
                messages=self.history,
            )

            text = response.content[0].text
            self.history.append({"role": "assistant", "content": text})

            alignment = self._record_turn(turn, text)
            trend = self._get_drift_trend()

            # Detect sustained drift: 3 consecutive low-alignment turns
            if len(trend) >= 3 and all(s < 0.15 for s in trend[:3]):
                correction = (
                    f"DRIFT ALERT (turn {turn}): Last 3 actions had low goal alignment "
                    f"(scores: {[round(s,2) for s in trend[:3]]}).\n"
                    f"REFOCUS: {self.goal}"
                )
                self.history.append({"role": "user", "content": correction})
                continue

            if any(kw in text.upper() for kw in ["GOAL COMPLETE", "TASK DONE", "FINISHED"]):
                self._complete_session("complete")
                return f"Goal completed at turn {turn}:\n{text}"

            self.history.append({
                "role": "user",
                "content": f"Turn {turn} alignment: {alignment:.2f}. Continue toward the goal."
            })

        self._complete_session("timeout")
        return f"Session timed out after {turns} turns. Review {self.DB_PATH} for drift analysis."


# Usage
agent = GoalJournalAgent(
    goal="Write unit tests for all functions in the authentication module",
    session_id="test-session-001"
)
result = agent.run()
print(result)

# Expected Token Savings: 20-40% by catching drift at turn 3 with forensic data for post-mortem
# Environment: Production agents, long-running batch tasks, any workflow requiring audit trails
```

## Comparison

| Option | Detection Method | Correction Speed | Overhead | Best For |
|--------|-----------------|------------------|----------|----------|
| Static Goal Injection | Always present | Immediate | Low (prompt size) | General use, simple tasks |
| Milestone Tracker | Completion tracking | Per milestone | Medium (Haiku checker) | Clear deliverable tasks |
| Drift Detector | LLM scoring every 3 turns | 3-turn lag | Medium (Haiku scorer) | Open-ended exploration |
| Goal Stack | Explicit push/pop | Immediate | Low | Hierarchical tasks |
| Periodic Summarization | Re-derivation every N turns | N-turn lag | Low-medium | Long research workflows |
| SQLite Goal Journal | Keyword alignment + trend | 3-turn lag | Low | Auditable production agents |
