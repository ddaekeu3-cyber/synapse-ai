---
title: "Agent Doesn't Implement Conversation Repair After Context Loss"
description: "When an AI agent's conversation context is lost mid-session — due to process restart, context window exhaustion, or session timeout — the agent starts from scratch and the user loses all prior conversation state."
category: reliability
difficulty: intermediate
tags: [context, conversation, repair, recovery, state, persistence, continuity, session]
---

# Agent Doesn't Implement Conversation Repair After Context Loss

## Problem

Agents running long conversations are vulnerable to context loss: the process restarts, the context window fills and is flushed, the session expires, or the client reconnects after a network drop. Without repair logic, the agent either starts a blank new conversation (disorienting the user) or hallucinates continuity it doesn't have (dangerous). Conversation repair means detecting context loss, recovering as much prior state as possible from durable storage, and re-orienting the model before resuming.

## Solution 1: Checkpoint-Based Context Recovery

Periodically checkpoint conversation state to durable storage. On restart, restore from the last checkpoint.

```python
import asyncio
import json
import time
from pathlib import Path
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

CHECKPOINT_DIR = Path("/tmp/agent_checkpoints")
CHECKPOINT_DIR.mkdir(exist_ok=True)

class ConversationCheckpointer:
    """
    Saves conversation state to disk after every N turns.
    Restores on reconnect so the agent resumes with full context.
    """

    def __init__(self, session_id: str, checkpoint_every: int = 5):
        self.session_id = session_id
        self.checkpoint_every = checkpoint_every
        self._path = CHECKPOINT_DIR / f"{session_id}.json"
        self._history: list[dict] = []
        self._turn_count = 0
        self._metadata: dict = {}

    def load(self) -> bool:
        """Load checkpoint if it exists. Returns True if restored."""
        if not self._path.exists():
            return False
        try:
            data = json.loads(self._path.read_text())
            self._history = data.get("history", [])
            self._metadata = data.get("metadata", {})
            self._turn_count = data.get("turn_count", 0)
            return bool(self._history)
        except (json.JSONDecodeError, KeyError):
            return False

    def save(self) -> None:
        data = {
            "session_id": self.session_id,
            "turn_count": self._turn_count,
            "saved_at": time.time(),
            "history": self._history,
            "metadata": self._metadata,
        }
        self._path.write_text(json.dumps(data, default=str))

    def add_turn(self, role: str, content: str, **meta) -> None:
        self._history.append({"role": role, "content": content})
        self._metadata.update(meta)
        self._turn_count += 1
        if self._turn_count % self.checkpoint_every == 0:
            self.save()

    @property
    def history(self) -> list[dict]:
        return list(self._history)

    @property
    def is_restored(self) -> bool:
        return self._turn_count > 0

    def delete(self) -> None:
        self._path.unlink(missing_ok=True)

async def agent_with_checkpoint_repair(session_id: str, user_message: str) -> dict:
    cp = ConversationCheckpointer(session_id, checkpoint_every=3)
    restored = cp.load()

    system = "You are a helpful assistant."
    if restored:
        # Inject re-orientation context so model knows it's resuming
        system += (
            f"\n\nNOTE: This conversation was interrupted and has been restored from checkpoint. "
            f"The conversation history below contains {len(cp.history)} prior messages. "
            f"Continue naturally from where the conversation left off."
        )

    cp.add_turn("user", user_message)

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=cp.history,
    )
    reply = resp.content[0].text
    cp.add_turn("assistant", reply)
    cp.save()

    return {
        "response": reply,
        "restored_from_checkpoint": restored,
        "turn_count": cp._turn_count,
    }
```

**When to use**: Any agent with sessions longer than a few minutes. Checkpointing is the minimal viable recovery mechanism.

---

## Solution 2: Rolling Summary — Compress History Before Loss

When the context window is approaching capacity, summarize the oldest messages into a compact summary that persists across the boundary.

```python
import asyncio
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

CONTEXT_TOKEN_LIMIT = 8000   # target limit before summarizing
SUMMARY_TRIGGER_TOKENS = 6000  # start summarizing at 75% of limit

def rough_token_count(messages: list[dict]) -> int:
    return sum(len(str(m.get("content", ""))) // 4 for m in messages)

async def summarize_history_segment(messages: list[dict]) -> str:
    """Compress a segment of conversation history into a brief summary."""
    history_text = "\n".join(
        f"{m['role'].upper()}: {m['content']}"
        for m in messages
    )
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": f"""Summarize this conversation segment in 3-5 sentences.
Capture: key decisions made, information shared, user goals, and current state.
Do not lose critical facts (names, numbers, commitments).

Conversation:
{history_text}

Summary:""",
        }],
    )
    return resp.content[0].text.strip()

class RollingContextManager:
    """
    Maintains a conversation history with automatic rolling summarization.
    When history exceeds SUMMARY_TRIGGER_TOKENS, oldest messages are
    summarized and replaced with a compact summary injection.
    """

    def __init__(self, system_prompt: str = ""):
        self.system_prompt = system_prompt
        self._history: list[dict] = []
        self._summary: str | None = None
        self._summary_covers_turns: int = 0

    async def add_and_get_context(self, role: str, content: str) -> tuple[list[dict], str]:
        """
        Add a message and return (history_to_send, system_to_send).
        Automatically summarizes if approaching context limit.
        """
        self._history.append({"role": role, "content": content})

        if rough_token_count(self._history) > SUMMARY_TRIGGER_TOKENS:
            await self._summarize_oldest()

        system = self.system_prompt
        if self._summary:
            system += (
                f"\n\n## Prior Conversation Summary\n"
                f"The following summarizes the conversation before this context window:\n"
                f"{self._summary}\n\n"
                f"(This summary covers approximately {self._summary_covers_turns} earlier turns.)"
            )

        return list(self._history), system

    async def _summarize_oldest(self) -> None:
        """Summarize the oldest half of the history and replace it."""
        split_at = len(self._history) // 2
        to_summarize = self._history[:split_at]
        to_keep = self._history[split_at:]

        new_summary_text = await summarize_history_segment(to_summarize)

        if self._summary:
            # Chain summaries: summarize the old summary + new segment
            combined = f"[Earlier summary]\n{self._summary}\n\n[Recent segment]\n{new_summary_text}"
            self._summary = combined
        else:
            self._summary = new_summary_text

        self._summary_covers_turns += split_at
        self._history = to_keep

ctx = RollingContextManager(system_prompt="You are a helpful assistant.")

async def agent_turn(user_message: str) -> str:
    messages, system = await ctx.add_and_get_context("user", user_message)
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=messages,
    )
    reply = resp.content[0].text
    await ctx.add_and_get_context("assistant", reply)
    return reply
```

**When to use**: Long conversations (>100 turns) where the context window is the binding constraint. Rolling summarization prevents context loss while preserving semantic continuity.

---

## Solution 3: Session Reconnection Protocol — Re-Orient the Model on Reconnect

When a client reconnects after a timeout and the in-memory context is gone, retrieve the stored history and inject an explicit re-orientation message.

```python
import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Optional
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@dataclass
class SessionRecord:
    session_id: str
    history: list[dict] = field(default_factory=list)
    user_metadata: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    interrupted: bool = False

_sessions: dict[str, SessionRecord] = {}  # In production: Redis / database

def save_session(record: SessionRecord) -> None:
    _sessions[record.session_id] = record
    record.last_active = time.time()

def load_session(session_id: str) -> Optional[SessionRecord]:
    return _sessions.get(session_id)

def build_reorientation_system(record: SessionRecord) -> str:
    gap_seconds = time.time() - record.last_active
    gap_str = f"{int(gap_seconds)}s" if gap_seconds < 60 else f"{int(gap_seconds/60)}min"

    base = "You are a helpful assistant."
    if record.history:
        last_user = next(
            (m["content"] for m in reversed(record.history) if m["role"] == "user"),
            None
        )
        last_assistant = next(
            (m["content"] for m in reversed(record.history) if m["role"] == "assistant"),
            None
        )
        context_note = (
            f"\n\nSESSION RESTORED: This session was interrupted {gap_str} ago. "
            f"You have {len(record.history)} prior messages of context. "
        )
        if last_user:
            context_note += f"\nThe user's last message was: \"{last_user[:200]}\""
        if last_assistant:
            context_note += f"\nYour last response was: \"{last_assistant[:200]}\""
        context_note += "\n\nContinue naturally. Do not re-introduce yourself."
        base += context_note
    return base

async def handle_reconnect(session_id: str, new_message: str) -> dict:
    record = load_session(session_id)
    is_new = record is None
    is_reconnect = record is not None and record.interrupted

    if is_new:
        record = SessionRecord(session_id=session_id)

    system = build_reorientation_system(record)
    record.history.append({"role": "user", "content": new_message})

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=record.history,
    )
    reply = resp.content[0].text
    record.history.append({"role": "assistant", "content": reply})
    record.interrupted = False
    save_session(record)

    return {
        "response": reply,
        "session_type": "new" if is_new else ("reconnect" if is_reconnect else "continue"),
        "history_turns": len(record.history),
    }

async def mark_session_interrupted(session_id: str) -> None:
    """Call this on disconnection/timeout."""
    record = load_session(session_id)
    if record:
        record.interrupted = True
        save_session(record)
```

**When to use**: Web-based agents where users navigate away and return. The re-orientation system message prevents the model from treating a restored session as a blank slate.

---

## Solution 4: Intent Replay — Re-Execute Lost Tool Calls from Stored Plan

When the agent loses context mid-task, replay the stored execution plan to restore intermediate tool results rather than re-asking the user.

```python
import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@dataclass
class ExecutionStep:
    step_id: str
    tool_name: str
    tool_args: dict
    result: dict | None = None
    completed: bool = False
    error: str | None = None

@dataclass
class TaskPlan:
    task_id: str
    goal: str
    steps: list[ExecutionStep] = field(default_factory=list)
    current_step: int = 0

    def next_incomplete(self) -> ExecutionStep | None:
        for step in self.steps:
            if not step.completed:
                return step
        return None

    @property
    def is_complete(self) -> bool:
        return all(s.completed for s in self.steps)

PLAN_DIR = Path("/tmp/agent_plans")
PLAN_DIR.mkdir(exist_ok=True)

def save_plan(plan: TaskPlan) -> None:
    path = PLAN_DIR / f"{plan.task_id}.json"
    path.write_text(json.dumps({
        "task_id": plan.task_id,
        "goal": plan.goal,
        "steps": [{
            "step_id": s.step_id,
            "tool_name": s.tool_name,
            "tool_args": s.tool_args,
            "result": s.result,
            "completed": s.completed,
            "error": s.error,
        } for s in plan.steps],
        "current_step": plan.current_step,
    }))

def load_plan(task_id: str) -> TaskPlan | None:
    path = PLAN_DIR / f"{task_id}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return TaskPlan(
        task_id=data["task_id"],
        goal=data["goal"],
        current_step=data["current_step"],
        steps=[ExecutionStep(**s) for s in data["steps"]],
    )

async def execute_tool(step: ExecutionStep) -> dict:
    """Simulate tool execution."""
    await asyncio.sleep(0.1)
    return {"tool": step.tool_name, "args": step.tool_args, "result": "success"}

async def resume_or_start_task(task_id: str, goal: str | None = None) -> dict:
    """Resume an interrupted task or start a new one."""
    plan = load_plan(task_id)

    if plan is None:
        if goal is None:
            return {"error": "no_plan_and_no_goal"}

        # Create new plan
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": f"Break this goal into 3 tool calls (JSON list of {{tool, args}} objects): {goal}",
            }],
        )
        plan = TaskPlan(task_id=task_id, goal=goal, steps=[
            ExecutionStep(step_id=f"step-{i}", tool_name=f"tool_{i}", tool_args={"i": i})
            for i in range(3)
        ])
        save_plan(plan)

    completed_before = sum(1 for s in plan.steps if s.completed)

    # Execute remaining steps
    while not plan.is_complete:
        step = plan.next_incomplete()
        if step is None:
            break
        step.result = await execute_tool(step)
        step.completed = True
        plan.current_step += 1
        save_plan(plan)  # persist after each step

    completed_after = sum(1 for s in plan.steps if s.completed)
    return {
        "task_id": task_id,
        "goal": plan.goal,
        "resumed": completed_before > 0,
        "steps_replayed": completed_before,
        "steps_completed_this_run": completed_after - completed_before,
        "total_steps": len(plan.steps),
    }
```

**When to use**: Multi-step agent tasks (research pipelines, code generation workflows) where re-executing expensive tool calls from scratch wastes time and money.

---

## Solution 5: User-Facing Repair Prompt — Ask the User to Re-Anchor

When context is unrecoverable and no checkpoint exists, ask the user a targeted re-anchoring question rather than presenting a blank slate.

```python
import asyncio
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

REPAIR_SYSTEM = """You are a helpful assistant that has just recovered from a session interruption.
You do not have access to the prior conversation.
Your task is to:
1. Acknowledge the interruption briefly and apologetically.
2. Ask ONE focused question to re-establish context (what the user was working on or needed).
3. Do not make up or assume prior context you don't have.
4. Be concise — one sentence of apology, one question."""

async def generate_repair_prompt(user_message: str) -> str:
    """
    Generate a targeted repair question when context is unrecoverable.
    The user's current message provides a clue about what they were doing.
    """
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=REPAIR_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"The user just sent this after a session interruption: \"{user_message}\"\n\nGenerate the repair prompt.",
        }],
    )
    return resp.content[0].text

async def agent_with_repair_prompt(
    session_id: str,
    user_message: str,
    has_context: bool,
    stored_history: list[dict] | None = None,
) -> dict:
    if has_context and stored_history:
        # Normal path: use stored history
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=stored_history + [{"role": "user", "content": user_message}],
        )
        return {"response": resp.content[0].text, "repair_mode": False}

    # Context lost and no checkpoint: generate repair prompt
    repair_question = await generate_repair_prompt(user_message)
    return {
        "response": repair_question,
        "repair_mode": True,
        "note": "Asking user to re-anchor conversation",
    }

# Example repair prompts:
# User: "Can you continue with step 3?"
# Repair: "I'm sorry, but our session was interrupted and I've lost the prior context.
#          Could you remind me what task or project we were working on, specifically what step 2 was?"

# User: "What about the other option you mentioned?"
# Repair: "Apologies for the interruption — I don't have access to our previous conversation.
#          Could you briefly describe what options we were comparing?"
```

**When to use**: Consumer-facing agents where checkpointing isn't implemented yet. A targeted repair prompt is always better than a blank "How can I help you?" that ignores the user's evident continuity expectation.

---

## Solution 6: Hybrid Recovery — Cascade Through Recovery Strategies

Try recovery strategies in order: in-memory → checkpoint → summary → repair prompt. Use the best available option.

```python
import asyncio
import json
import time
from pathlib import Path
from enum import Enum
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

class RecoveryStrategy(Enum):
    IN_MEMORY    = "in_memory"     # full history available
    CHECKPOINT   = "checkpoint"    # restored from disk checkpoint
    SUMMARY_ONLY = "summary_only"  # only a summary is available
    REPAIR_PROMPT = "repair_prompt" # nothing available, ask user

CHECKPOINT_PATH = Path("/tmp/agent_hybrid_checkpoints")
CHECKPOINT_PATH.mkdir(exist_ok=True)

_memory_sessions: dict[str, list[dict]] = {}

async def recover_session(session_id: str, user_message: str) -> dict:
    """
    Try recovery strategies in order; use the best available.
    """
    history = None
    summary = None
    strategy = RecoveryStrategy.REPAIR_PROMPT

    # 1. Check in-memory
    if session_id in _memory_sessions:
        history = _memory_sessions[session_id]
        strategy = RecoveryStrategy.IN_MEMORY

    # 2. Check checkpoint
    if history is None:
        cp_path = CHECKPOINT_PATH / f"{session_id}.json"
        if cp_path.exists():
            try:
                data = json.loads(cp_path.read_text())
                history = data.get("history", [])
                summary = data.get("summary")
                age = time.time() - data.get("saved_at", 0)
                strategy = RecoveryStrategy.CHECKPOINT
                if not history and summary:
                    strategy = RecoveryStrategy.SUMMARY_ONLY
            except Exception:
                pass

    # Build system and messages based on strategy
    system = "You are a helpful assistant."
    messages = []

    if strategy == RecoveryStrategy.IN_MEMORY:
        messages = history + [{"role": "user", "content": user_message}]

    elif strategy == RecoveryStrategy.CHECKPOINT:
        if summary:
            system += f"\n\n## Conversation Summary (prior context)\n{summary}"
        messages = (history or []) + [{"role": "user", "content": user_message}]
        system += "\n\nNOTE: Session was restored from checkpoint. Continue naturally."

    elif strategy == RecoveryStrategy.SUMMARY_ONLY:
        system += f"\n\n## Prior Conversation Summary\n{summary}\n\nNOTE: Detailed history unavailable; only summary exists."
        messages = [{"role": "user", "content": user_message}]

    else:  # REPAIR_PROMPT
        system = """You are a helpful assistant recovering from a session interruption.
Acknowledge briefly, then ask one focused question to re-establish what the user needs."""
        messages = [{"role": "user", "content": user_message}]

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=messages,
    )
    reply = resp.content[0].text

    # Update in-memory and persist checkpoint
    if history is None:
        history = []
    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": reply})
    _memory_sessions[session_id] = history

    cp_path = CHECKPOINT_PATH / f"{session_id}.json"
    cp_path.write_text(json.dumps({
        "history": history[-20:],  # keep last 20 turns
        "saved_at": time.time(),
    }))

    return {
        "response": reply,
        "recovery_strategy": strategy.value,
        "history_turns": len(history),
    }
```

**When to use**: Production agents where you want maximum resilience. The cascade ensures you always use the best available recovery option without hard-coding assumptions about what will be available.

---

## Comparison

| Solution | Context Fidelity | Setup Complexity | User Disruption | Cost | Best For |
|---|---|---|---|---|---|
| Checkpoint recovery | High | Low | Minimal | ~0% | All agents (baseline) |
| Rolling summary | Medium | Medium | None | 1–5% (summarization) | Long conversations |
| Session reconnect protocol | High | Medium | Minimal | ~0% | Web-based agents |
| Intent replay | High | High | None | Varies (tool re-exec) | Multi-step task agents |
| Repair prompt | Low | Very low | Moderate | ~0% | No checkpoint available |
| Hybrid cascade | Highest | Medium | Minimal | ~0% | Production systems |

**Rule of thumb**: Always checkpoint after every turn (Solution 1) — it's free and enables all other recovery strategies. Add rolling summarization (Solution 2) for long sessions. Add the hybrid cascade (Solution 6) in production so you never need to ask the user "what were we talking about?"
