---
layout: solution
title: "Agent Doesn't Implement Working Memory vs Long-Term Memory Separation"
category: memory
description: "Agent stores everything in a single flat context, causing expensive token bloat, loss of task focus, and inability to recall past sessions — all fixed by separating short-lived working memory from persistent long-term memory."
tags: [memory, working-memory, long-term-memory, context-management, sqlite, embeddings]
---

# Agent Doesn't Implement Working Memory vs Long-Term Memory Separation

## Problem

Most agents treat memory as a single undifferentiated context blob: everything from the current task, past conversations, user preferences, and retrieved facts gets stuffed into the same message list. This causes:

- **Context bloat**: token cost grows quadratically as old facts accumulate
- **Lost focus**: critical current-task state buried under historical noise
- **Amnesia across sessions**: working context is discarded when the conversation ends
- **No recall**: genuinely important facts from past sessions are never surfaced again

**Root cause:** No separation between ephemeral working memory (current task state) and durable long-term memory (facts worth keeping across sessions).

**The fix:** Maintain two distinct memory stores — working memory (fast, in-process, task-scoped) and long-term memory (persistent, retrievable, session-agnostic).

---

## Option 1: Dictionary-Based Working Memory + SQLite Long-Term Memory

Simplest split: Python dict for current task, SQLite for durable facts.

```python
import anthropic
import json
import sqlite3
import time
from pathlib import Path
from dataclasses import dataclass, field

client = anthropic.Anthropic()
DB_PATH = Path("/tmp/agent_ltm.db")

def init_ltm() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS long_term_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            category TEXT DEFAULT 'general',
            importance REAL DEFAULT 0.5,
            created_at REAL DEFAULT (unixepoch()),
            accessed_at REAL DEFAULT (unixepoch()),
            access_count INTEGER DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_key ON long_term_memory(key)")
    conn.commit()
    return conn

@dataclass
class WorkingMemory:
    """Short-lived, task-scoped memory. Discarded at task end."""
    task_id: str
    created_at: float = field(default_factory=time.time)
    slots: dict[str, any] = field(default_factory=dict)
    tool_results: list[dict] = field(default_factory=list)

    def set(self, key: str, value: any):
        self.slots[key] = value

    def get(self, key: str, default=None):
        return self.slots.get(key, default)

    def add_tool_result(self, tool_name: str, result: dict):
        self.tool_results.append({"tool": tool_name, "result": result, "at": time.time()})

    def to_context_snippet(self) -> str:
        if not self.slots and not self.tool_results:
            return ""
        parts = ["[Working Memory]"]
        for k, v in self.slots.items():
            parts.append(f"  {k}: {v}")
        if self.tool_results:
            parts.append(f"  Recent tool calls: {len(self.tool_results)}")
        return "\n".join(parts)

class LongTermMemory:
    """Durable, session-persistent memory stored in SQLite."""
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def store(self, key: str, value: str, category: str = "general", importance: float = 0.5):
        self.conn.execute("""
            INSERT INTO long_term_memory (key, value, category, importance)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, importance=excluded.importance, accessed_at=unixepoch()
        """, (key, value, category, importance))
        self.conn.commit()

    def recall(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM long_term_memory WHERE key = ?", (key,)
        ).fetchone()
        if row:
            self.conn.execute(
                "UPDATE long_term_memory SET accessed_at=unixepoch(), access_count=access_count+1 WHERE key=?", (key,)
            )
            self.conn.commit()
            return row[0]
        return None

    def recall_by_category(self, category: str, limit: int = 5) -> list[dict]:
        rows = self.conn.execute(
            "SELECT key, value, importance FROM long_term_memory WHERE category=? ORDER BY importance DESC, accessed_at DESC LIMIT ?",
            (category, limit)
        ).fetchall()
        return [{"key": r[0], "value": r[1], "importance": r[2]} for r in rows]

    def to_context_snippet(self, category: str | None = None) -> str:
        if category:
            items = self.recall_by_category(category)
        else:
            rows = self.conn.execute(
                "SELECT key, value FROM long_term_memory ORDER BY importance DESC, accessed_at DESC LIMIT 5"
            ).fetchall()
            items = [{"key": r[0], "value": r[1]} for r in rows]
        if not items:
            return ""
        parts = ["[Long-Term Memory]"]
        for item in items:
            parts.append(f"  {item['key']}: {item['value']}")
        return "\n".join(parts)

conn = init_ltm()
ltm = LongTermMemory(conn)

# Pre-populate some long-term facts
ltm.store("user_name", "Alex", category="user", importance=0.9)
ltm.store("user_language", "English", category="user", importance=0.8)
ltm.store("preferred_currency", "USD", category="preferences", importance=0.7)

tools = [
    {
        "name": "save_to_memory",
        "description": "Save an important fact to long-term memory",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": {"type": "string"},
                "category": {"type": "string"},
                "importance": {"type": "number"}
            },
            "required": ["key", "value"]
        }
    },
    {
        "name": "recall_from_memory",
        "description": "Recall a specific fact from long-term memory",
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"]
        }
    }
]

def run_agent_with_memory_split(user_query: str, task_id: str = "task-001") -> str:
    wm = WorkingMemory(task_id=task_id)
    wm.set("current_query", user_query)

    ltm_context = ltm.to_context_snippet()
    system = f"""You are a helpful assistant with two-tier memory.
{ltm_context}

Use save_to_memory for facts that should persist across sessions.
Use working memory (already injected above) for current task state."""

    messages = [{"role": "user", "content": user_query}]

    while True:
        wm_context = wm.to_context_snippet()
        if wm_context:
            enriched = messages.copy()
            enriched[0] = {"role": "user", "content": f"{wm_context}\n\nUser: {user_query}"}
        else:
            enriched = messages

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            tools=tools,
            messages=enriched
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            if block.name == "save_to_memory":
                ltm.store(
                    block.input["key"],
                    block.input["value"],
                    block.input.get("category", "general"),
                    block.input.get("importance", 0.5)
                )
                result = {"status": "saved", "key": block.input["key"]}
                wm.add_tool_result("save_to_memory", result)
            elif block.name == "recall_from_memory":
                value = ltm.recall(block.input["key"])
                result = {"key": block.input["key"], "value": value or "not found"}
                wm.add_tool_result("recall_from_memory", result)
            else:
                result = {"error": f"Unknown tool: {block.name}"}

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result)
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return "Done"

print(run_agent_with_memory_split("My favorite city is Tokyo. Please remember that."))
print(run_agent_with_memory_split("What do you know about my preferences?"))

# Expected Token Savings: ~40% (LTM injects only top-N relevant facts; WM holds only current task state)
# Environment: Personal assistant agents, customer service bots, any multi-session agent
```

---

## Option 2: Priority-Based Working Memory with Eviction

Working memory with a priority queue that evicts low-importance slots when capacity is reached.

```python
import anthropic
import json
import heapq
from dataclasses import dataclass, field
from typing import Any

client = anthropic.Anthropic()

@dataclass(order=True)
class MemorySlot:
    priority: float          # Higher = more important
    key: str = field(compare=False)
    value: Any = field(compare=False)
    access_count: int = field(compare=False, default=0)

    def __lt__(self, other):
        return self.priority < other.priority  # Min-heap: lowest priority evicted first

class BoundedWorkingMemory:
    """Fixed-capacity working memory with LFU+priority eviction."""
    def __init__(self, max_slots: int = 8):
        self.max_slots = max_slots
        self._slots: dict[str, MemorySlot] = {}

    def write(self, key: str, value: Any, priority: float = 0.5):
        if key in self._slots:
            slot = self._slots[key]
            slot.value = value
            slot.priority = max(slot.priority, priority)
            slot.access_count += 1
            return

        if len(self._slots) >= self.max_slots:
            # Evict lowest-priority slot
            evict_key = min(self._slots, key=lambda k: (self._slots[k].priority, self._slots[k].access_count))
            print(f"[WM evict] Dropping '{evict_key}' (priority={self._slots[evict_key].priority:.2f})")
            del self._slots[evict_key]

        self._slots[key] = MemorySlot(priority=-priority, key=key, value=value)

    def read(self, key: str) -> Any:
        slot = self._slots.get(key)
        if slot:
            slot.access_count += 1
            return slot.value
        return None

    def top_k(self, k: int = 5) -> list[tuple[str, Any]]:
        sorted_slots = sorted(self._slots.values(), key=lambda s: -s.priority)
        return [(s.key, s.value) for s in sorted_slots[:k]]

    def to_context(self) -> str:
        items = self.top_k()
        if not items:
            return ""
        lines = ["[Active Working Memory]"]
        for key, value in items:
            lines.append(f"  {key}: {value}")
        return "\n".join(lines)

    @property
    def utilization(self) -> str:
        return f"{len(self._slots)}/{self.max_slots}"

# Long-term memory (simple dict for this example)
_long_term_store: dict[str, str] = {
    "user_name": "Jordan",
    "account_tier": "premium",
    "timezone": "America/New_York"
}

tools = [
    {
        "name": "set_working_memory",
        "description": "Store a value in working memory for the current task",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": {"type": "string"},
                "priority": {"type": "number", "description": "0.0-1.0, higher = more important"}
            },
            "required": ["key", "value"]
        }
    },
    {
        "name": "get_working_memory",
        "description": "Read a value from working memory",
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"]
        }
    }
]

def run_agent_bounded_wm(query: str) -> str:
    wm = BoundedWorkingMemory(max_slots=8)

    # Inject top long-term facts as low-priority WM slots
    for k, v in _long_term_store.items():
        wm.write(k, v, priority=0.3)

    system = f"""You are a focused assistant. Track important task state with set_working_memory.
{wm.to_context()}"""

    messages = [{"role": "user", "content": query}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            tools=tools,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            print(f"[WM utilization] {wm.utilization}")
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            if block.name == "set_working_memory":
                wm.write(block.input["key"], block.input["value"], block.input.get("priority", 0.5))
                result = {"status": "stored", "wm_utilization": wm.utilization}
            elif block.name == "get_working_memory":
                value = wm.read(block.input["key"])
                result = {"key": block.input["key"], "value": value or "not in working memory"}
            else:
                result = {"error": "Unknown tool"}

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result)
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return "Done"

print(run_agent_bounded_wm("Help me plan a 3-step data pipeline for sales analysis"))

# Expected Token Savings: ~35% (bounded WM prevents unbounded context growth; only top-priority facts injected)
# Environment: Long-running planning agents, data pipeline builders, research assistants
```

---

## Option 3: Episodic Memory — Summarize and Compress Working Memory to LTM

After each task, summarize working memory into compressed episodes stored in long-term memory.

```python
import anthropic
import json
import sqlite3
from pathlib import Path
from datetime import datetime

client = anthropic.Anthropic()
EPISODE_DB = Path("/tmp/agent_episodes.db")

def init_episode_db() -> sqlite3.Connection:
    conn = sqlite3.connect(EPISODE_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_description TEXT,
            summary TEXT NOT NULL,
            key_facts TEXT,  -- JSON list
            outcome TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            tags TEXT DEFAULT ''
        )
    """)
    conn.commit()
    return conn

def compress_working_memory_to_episode(
    task_description: str,
    working_memory_dump: dict,
    tool_calls_summary: list[str]
) -> dict:
    """Use LLM to summarize a completed task into a compact episode."""
    prompt = f"""Summarize this completed agent task into a compact episode for long-term memory.

Task: {task_description}
Working memory contents: {json.dumps(working_memory_dump, indent=2)}
Tool calls made: {json.dumps(tool_calls_summary)}

Return JSON with:
- summary: one paragraph (max 3 sentences)
- key_facts: list of up to 5 bullet-point facts worth remembering
- outcome: "success" | "partial" | "failed"
- tags: list of relevant topic tags"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}]
    )

    try:
        return json.loads(response.content[0].text)
    except (json.JSONDecodeError, IndexError):
        return {
            "summary": f"Task completed: {task_description}",
            "key_facts": list(working_memory_dump.values())[:3],
            "outcome": "success",
            "tags": []
        }

def store_episode(conn: sqlite3.Connection, task_desc: str, episode: dict):
    conn.execute("""
        INSERT INTO episodes (task_description, summary, key_facts, outcome, tags)
        VALUES (?, ?, ?, ?, ?)
    """, (
        task_desc,
        episode.get("summary", ""),
        json.dumps(episode.get("key_facts", [])),
        episode.get("outcome", "unknown"),
        json.dumps(episode.get("tags", []))
    ))
    conn.commit()

def recall_recent_episodes(conn: sqlite3.Connection, limit: int = 3) -> str:
    rows = conn.execute(
        "SELECT task_description, summary, key_facts FROM episodes ORDER BY id DESC LIMIT ?",
        (limit,)
    ).fetchall()
    if not rows:
        return ""
    lines = ["[Episodic Memory — Recent Tasks]"]
    for task, summary, facts_json in rows:
        lines.append(f"  Task: {task}")
        lines.append(f"  Summary: {summary}")
        try:
            facts = json.loads(facts_json)
            for f in facts[:2]:
                lines.append(f"    • {f}")
        except Exception:
            pass
    return "\n".join(lines)

conn = init_episode_db()

def run_agent_with_episodic_memory(task: str) -> str:
    working_memory: dict = {"task": task, "steps_completed": [], "artifacts": []}
    tool_calls: list[str] = []

    episode_context = recall_recent_episodes(conn)
    system = f"""You are an agent with episodic memory.
{episode_context}

Complete the current task and track progress carefully."""

    messages = [{"role": "user", "content": task}]

    tools = [
        {
            "name": "log_step",
            "description": "Log a completed step to working memory",
            "input_schema": {
                "type": "object",
                "properties": {
                    "step": {"type": "string"},
                    "artifact": {"type": "string"}
                },
                "required": ["step"]
            }
        }
    ]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            tools=tools,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            final_text = next(b.text for b in response.content if hasattr(b, "text"))

            # Compress WM to episodic LTM
            episode = compress_working_memory_to_episode(task, working_memory, tool_calls)
            store_episode(conn, task, episode)
            print(f"[episodic] Stored episode: {episode['summary'][:80]}...")

            return final_text

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            if block.name == "log_step":
                step = block.input["step"]
                artifact = block.input.get("artifact", "")
                working_memory["steps_completed"].append(step)
                if artifact:
                    working_memory["artifacts"].append(artifact)
                tool_calls.append(f"log_step: {step}")
                result = {"logged": step}
            else:
                result = {"error": "Unknown tool"}

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result)
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return "Done"

print(run_agent_with_episodic_memory("Analyze sales data for Q1 and identify top 3 trends"))
print(run_agent_with_episodic_memory("Write a Python script to automate daily reports"))

# Expected Token Savings: ~50% (episode summaries are 90% smaller than raw WM; injected selectively)
# Environment: Long-horizon agents doing repeated tasks; coding assistants; research agents
```

---

## Option 4: Attention-Based Memory Routing

Route new information to WM or LTM automatically based on importance classification.

```python
import anthropic
import json
import sqlite3
from pathlib import Path
from enum import Enum

client = anthropic.Anthropic()
ROUTING_DB = Path("/tmp/agent_routing_mem.db")

class MemoryTier(Enum):
    WORKING = "working"       # Current task only
    LONG_TERM = "long_term"  # Persist across sessions
    DISCARD = "discard"      # Not worth keeping

def classify_memory_tier(fact: str, context: str) -> MemoryTier:
    """Use LLM to classify where a fact should live."""
    prompt = f"""Classify where this fact should be stored:

Fact: {fact}
Context: {context}

Rules:
- "working": task-specific, needed now, disposable after task ends
- "long_term": user preference, identity, repeated pattern, long-lived truth
- "discard": irrelevant, transient, already in context

Reply with ONLY one word: working, long_term, or discard"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        messages=[{"role": "user", "content": prompt}]
    )

    answer = response.content[0].text.strip().lower()
    if "long" in answer:
        return MemoryTier.LONG_TERM
    if "discard" in answer:
        return MemoryTier.DISCARD
    return MemoryTier.WORKING

def init_routing_db() -> sqlite3.Connection:
    conn = sqlite3.connect(ROUTING_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ltm_facts (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn

conn = init_routing_db()
working_memory_store: dict[str, str] = {}

def store_fact(key: str, value: str, tier: MemoryTier):
    if tier == MemoryTier.WORKING:
        working_memory_store[key] = value
        print(f"[routing] WM ← {key}: {value[:40]}")
    elif tier == MemoryTier.LONG_TERM:
        conn.execute("INSERT OR REPLACE INTO ltm_facts (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
        print(f"[routing] LTM ← {key}: {value[:40]}")
    else:
        print(f"[routing] DISCARD: {key}")

tools = [
    {
        "name": "remember_fact",
        "description": "Store a fact — system will route it to working or long-term memory automatically",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": {"type": "string"}
            },
            "required": ["key", "value"]
        }
    }
]

def run_agent_with_routing(query: str) -> str:
    ltm_rows = conn.execute("SELECT key, value FROM ltm_facts LIMIT 5").fetchall()
    ltm_context = "\n".join(f"  {k}: {v}" for k, v in ltm_rows)
    system = f"""You are an assistant with intelligent memory routing.
Long-term memory:
{ltm_context or '  (empty)'}

Use remember_fact to store important information — the system routes it automatically."""

    messages = [{"role": "user", "content": query}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            tools=tools,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            if block.name == "remember_fact":
                tier = classify_memory_tier(
                    fact=f"{block.input['key']}: {block.input['value']}",
                    context=query
                )
                store_fact(block.input["key"], block.input["value"], tier)
                result = {"stored": True, "tier": tier.value}
            else:
                result = {"error": "Unknown tool"}

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result)
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return "Done"

print(run_agent_with_routing("I prefer dark mode and my name is Sam. Help me debug this Python script."))

# Expected Token Savings: ~30% (LLM-routed facts prevent over-persisting transient WM into LTM)
# Environment: Personal agents where distinguishing user preferences from task state is critical
```

---

## Option 5: Structured Working Memory with Slot Types

Define named WM slots with typed fields (goal, plan, current_step, etc.) for structured task tracking.

```python
import anthropic
import json
from dataclasses import dataclass, field, asdict
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class StructuredWorkingMemory:
    """Typed slots enforce clarity about what belongs in working memory."""
    # Goal tracking
    primary_goal: str = ""
    sub_goals: list[str] = field(default_factory=list)

    # Plan tracking
    plan_steps: list[str] = field(default_factory=list)
    current_step_index: int = 0
    completed_steps: list[str] = field(default_factory=list)

    # Context
    entities_mentioned: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)

    # Scratchpad for intermediate results
    scratchpad: dict[str, str] = field(default_factory=dict)

    @property
    def current_step(self) -> Optional[str]:
        if self.current_step_index < len(self.plan_steps):
            return self.plan_steps[self.current_step_index]
        return None

    def advance_step(self) -> bool:
        if self.current_step:
            self.completed_steps.append(self.current_step)
            self.current_step_index += 1
            return True
        return False

    def to_context(self) -> str:
        lines = ["[Working Memory]"]
        if self.primary_goal:
            lines.append(f"  Goal: {self.primary_goal}")
        if self.current_step:
            lines.append(f"  Current step ({self.current_step_index + 1}/{len(self.plan_steps)}): {self.current_step}")
        if self.completed_steps:
            lines.append(f"  Completed: {', '.join(self.completed_steps[-2:])}")
        if self.open_questions:
            lines.append(f"  Open questions: {'; '.join(self.open_questions[:2])}")
        if self.constraints:
            lines.append(f"  Constraints: {'; '.join(self.constraints)}")
        for k, v in list(self.scratchpad.items())[-3:]:
            lines.append(f"  scratch.{k}: {v[:60]}")
        return "\n".join(lines)

wm = StructuredWorkingMemory()

tools = [
    {
        "name": "update_working_memory",
        "description": "Update structured working memory fields",
        "input_schema": {
            "type": "object",
            "properties": {
                "field": {
                    "type": "string",
                    "enum": ["primary_goal", "add_sub_goal", "set_plan", "advance_step",
                             "add_question", "add_constraint", "set_scratchpad"]
                },
                "value": {"type": "string"}
            },
            "required": ["field"]
        }
    }
]

def apply_wm_update(wm: StructuredWorkingMemory, field: str, value: str = "") -> dict:
    if field == "primary_goal":
        wm.primary_goal = value
    elif field == "add_sub_goal":
        wm.sub_goals.append(value)
    elif field == "set_plan":
        try:
            steps = json.loads(value)
            wm.plan_steps = steps if isinstance(steps, list) else [value]
        except Exception:
            wm.plan_steps = [s.strip() for s in value.split(",")]
    elif field == "advance_step":
        wm.advance_step()
    elif field == "add_question":
        wm.open_questions.append(value)
    elif field == "add_constraint":
        wm.constraints.append(value)
    elif field == "set_scratchpad":
        try:
            kv = json.loads(value)
            wm.scratchpad.update(kv)
        except Exception:
            wm.scratchpad["note"] = value
    return {"updated": field, "wm_snapshot": wm.to_context()}

def run_agent_structured_wm(query: str) -> str:
    system = "You are a structured planning agent. Use update_working_memory to track your goal, plan, and progress."
    messages = [{"role": "user", "content": query}]

    while True:
        wm_context = wm.to_context()
        enriched_system = f"{system}\n\n{wm_context}" if wm_context.strip() != "[Working Memory]" else system

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=768,
            system=enriched_system,
            tools=tools,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            if block.name == "update_working_memory":
                result = apply_wm_update(wm, block.input["field"], block.input.get("value", ""))
            else:
                result = {"error": "Unknown tool"}

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result)
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return "Done"

print(run_agent_structured_wm("Plan and outline a 5-chapter book about AI agent architectures"))

# Expected Token Savings: ~20% (typed WM fields prevent redundant re-stating of plan/goal in every message)
# Environment: Planning agents, project management bots, multi-step workflow executors
```

---

## Option 6: Full Two-Tier Memory with Embedding-Based LTM Retrieval

Production-grade: semantic search over long-term memory to inject only the most relevant past facts.

```python
import anthropic
import json
import sqlite3
import math
from pathlib import Path

client = anthropic.Anthropic()
FULL_MEM_DB = Path("/tmp/agent_full_memory.db")

def init_full_db() -> sqlite3.Connection:
    conn = sqlite3.connect(FULL_MEM_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ltm_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            embedding TEXT,  -- JSON list of floats
            category TEXT DEFAULT 'general',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn

def fake_embed(text: str) -> list[float]:
    """Deterministic fake embedding based on character frequencies."""
    vec = [0.0] * 16
    for i, ch in enumerate(text.lower()):
        vec[ord(ch) % 16] += 1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]

def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0

def store_ltm(conn: sqlite3.Connection, content: str, category: str = "general"):
    embedding = fake_embed(content)
    conn.execute(
        "INSERT INTO ltm_entries (content, embedding, category) VALUES (?, ?, ?)",
        (content, json.dumps(embedding), category)
    )
    conn.commit()

def retrieve_relevant_ltm(conn: sqlite3.Connection, query: str, top_k: int = 3) -> list[str]:
    query_emb = fake_embed(query)
    rows = conn.execute("SELECT content, embedding FROM ltm_entries").fetchall()
    if not rows:
        return []
    scored = []
    for content, emb_json in rows:
        emb = json.loads(emb_json)
        score = cosine_similarity(query_emb, emb)
        scored.append((score, content))
    scored.sort(reverse=True)
    return [content for _, content in scored[:top_k]]

conn = init_full_db()
working_memory: dict[str, str] = {}

# Seed some long-term memories
store_ltm(conn, "User prefers concise answers without unnecessary preamble", "preferences")
store_ltm(conn, "User is a senior Python developer familiar with async/await", "user_profile")
store_ltm(conn, "Previous task: built a FastAPI service with JWT auth", "history")
store_ltm(conn, "User's timezone is UTC+9 (Seoul/Tokyo)", "user_profile")

tools = [
    {
        "name": "store_memory",
        "description": "Store a fact. Use category='working' for task-specific, 'long_term' for persistent facts.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "category": {"type": "string", "enum": ["working", "long_term"]}
            },
            "required": ["content", "category"]
        }
    }
]

def run_agent_full_two_tier(query: str) -> str:
    # Retrieve semantically relevant LTM entries
    relevant = retrieve_relevant_ltm(conn, query, top_k=3)
    ltm_block = "\n".join(f"  • {r}" for r in relevant) if relevant else "  (none relevant)"

    # Working memory block
    wm_block = "\n".join(f"  {k}: {v}" for k, v in working_memory.items()) if working_memory else "  (empty)"

    system = f"""You are an assistant with two-tier memory.

[Long-Term Memory — most relevant to current query]
{ltm_block}

[Working Memory — current task]
{wm_block}

Use store_memory to save important facts (specify 'working' or 'long_term')."""

    messages = [{"role": "user", "content": query}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            tools=tools,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            if block.name == "store_memory":
                content = block.input["content"]
                category = block.input["category"]
                if category == "long_term":
                    store_ltm(conn, content, "general")
                    result = {"stored": "long_term", "content": content[:50]}
                else:
                    key = f"wm_{len(working_memory)}"
                    working_memory[key] = content
                    result = {"stored": "working", "key": key}
            else:
                result = {"error": "Unknown tool"}

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result)
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return "Done"

print(run_agent_full_two_tier("Help me add rate limiting to my FastAPI service"))

# Expected Token Savings: ~60% (semantic retrieval injects only top-3 relevant LTM facts vs. full history dump)
# Environment: Production agents with months of accumulated knowledge; customer-facing assistants
```

---

## Comparison

| Option | WM Storage | LTM Storage | Retrieval | Eviction | Best For |
|--------|-----------|-------------|-----------|----------|----------|
| 1. Dict + SQLite | Python dict | SQLite rows | Key lookup | Manual | Simple multi-session agents |
| 2. Priority Queue WM | Min-heap dict | External | N/A | Auto LFU+priority | Long tasks with many facts |
| 3. Episodic | Dict | SQLite episodes | Recent episodes | N/A — compressed | Repeated task workflows |
| 4. Attention Routing | Dict | SQLite | LLM-classified | Auto routing | Personal assistants |
| 5. Structured Typed | Dataclass slots | External | N/A | N/A | Planning and multi-step agents |
| 6. Embedding Retrieval | Dict | SQLite + vectors | Semantic cosine | N/A | Production knowledge-rich agents |
