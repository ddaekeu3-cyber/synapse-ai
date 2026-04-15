---
layout: solution
title: "Agent Writes Too Much to Working Memory — Context Fills with Intermediate State"
category: memory
description: "Agent stores every intermediate result, calculation, and scratchpad note in the conversation context as working memory. By turn 10, the context is full of stale intermediate state. Model slows down and costs spike."
tags: [working-memory, context, intermediate-state, scratchpad, bloat, token-cost, memory]
---

# Agent Writes Too Much to Working Memory — Context Fills with Intermediate State

## Symptom
- Agent writes step-by-step calculations, notes, and intermediate results into the conversation
- Context window fills after 10-15 turns of a complex task
- Older working notes from early task steps take up space needed for current work
- Model processes 50,000 tokens of intermediate state that's no longer relevant
- Every turn costs more as accumulated working memory grows
- Agent "thinks out loud" verbosely instead of storing just the final conclusions

## Root Cause
When agents use in-context working memory (writing notes and calculations directly into the conversation), all intermediate state persists forever. Turn 3 calculations are still in context at turn 20, even if they're no longer needed. This is appropriate for tracking progress, but wasteful when intermediate steps can be discarded once conclusions are reached.

## Fix

### Option 1: Scratchpad that compresses itself after each step

```python
class CompactingScratchpad:
    """
    Working memory that compresses old entries into conclusions.
    Intermediate steps are discarded once they've produced a final result.
    """

    def __init__(self, max_entries: int = 5):
        self.max_entries = max_entries
        self._entries: list[dict] = []
        self._conclusions: list[str] = []

    def note(self, content: str, is_intermediate: bool = True):
        """Add a working note"""
        self._entries.append({
            "content": content,
            "intermediate": is_intermediate
        })

    def conclude(self, conclusion: str):
        """Record a conclusion and drop intermediate steps that led to it"""
        self._conclusions.append(conclusion)
        # Drop intermediate entries — their work is captured in the conclusion
        self._entries = [e for e in self._entries if not e["intermediate"]]

    def prune(self):
        """Keep only max_entries most recent entries"""
        if len(self._entries) > self.max_entries:
            dropped = len(self._entries) - self.max_entries
            print(f"Scratchpad pruned {dropped} old entries")
            self._entries = self._entries[-self.max_entries:]

    def as_context(self) -> str:
        """Compact context string to inject into next turn"""
        lines = []
        if self._conclusions:
            lines.append("Established conclusions:")
            for c in self._conclusions[-3:]:  # Last 3 conclusions only
                lines.append(f"  ✓ {c}")
        if self._entries:
            lines.append("Working notes:")
            for e in self._entries:
                lines.append(f"  - {e['content']}")
        return "\n".join(lines) if lines else "No working notes."

pad = CompactingScratchpad()
pad.note("Total revenue = $1.2M, costs = $800K")  # intermediate
pad.note("Margin calculation: 1200000 - 800000 = 400000")  # intermediate
pad.conclude("Gross margin is $400K (33.3%)")  # Drops the intermediates above
# Context now has just the conclusion, not the calculation steps
```

### Option 2: External working memory store (outside context)

```python
from pathlib import Path
import json

class ExternalWorkingMemory:
    """
    Store working state in a file/database instead of the conversation.
    Agent reads/writes via tool calls — context only contains summaries.
    """

    def __init__(self, task_id: str, storage_dir: str = "/tmp/agent_memory"):
        self.task_id = task_id
        self.path = Path(storage_dir) / f"{task_id}.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict = {}
        if self.path.exists():
            self._data = json.loads(self.path.read_text())

    def write(self, key: str, value):
        """Store a value — not in context, in file"""
        self._data[key] = value
        self.path.write_text(json.dumps(self._data, indent=2))

    def read(self, key: str, default=None):
        """Read a value from external store"""
        return self._data.get(key, default)

    def summary(self) -> str:
        """Compact summary for injecting into context"""
        if not self._data:
            return "No working state stored."
        keys = list(self._data.keys())
        return (
            f"Working state has {len(keys)} entries: {', '.join(keys[:5])}"
            + (f" and {len(keys)-5} more" if len(keys) > 5 else "")
        )

# In agent tools:
memory = ExternalWorkingMemory(task_id="task_abc123")

def store_result_tool(key: str, value: str) -> str:
    """Agent tool: save to external memory, not into context"""
    memory.write(key, value)
    return f"Stored '{key}' externally. {memory.summary()}"

def read_result_tool(key: str) -> str:
    """Agent tool: read from external memory"""
    value = memory.read(key)
    return f"{key} = {value}" if value is not None else f"'{key}' not found"

# Context contains only: "Working state has 12 entries: revenue, costs, margin, ..."
# Full data is in the file, not the context
```

### Option 3: Summarize working memory at regular intervals

```python
async def compress_working_memory(
    working_memory: str,
    agent,
    max_chars: int = 500
) -> str:
    """
    When working memory grows too large, summarize it.
    Keep key conclusions, drop intermediate calculations.
    """
    if len(working_memory) <= max_chars:
        return working_memory

    summary = await agent.call([{
        "role": "user",
        "content": (
            f"Compress this working memory into the key conclusions and current state. "
            f"Keep: decisions made, values established, current step. "
            f"Drop: intermediate calculations, abandoned approaches, duplicate notes. "
            f"Max {max_chars} characters.\n\n"
            f"Working memory:\n{working_memory}"
        )
    }])

    print(f"Working memory compressed: {len(working_memory)} → {len(summary)} chars")
    return summary

# Run compression every 5 turns:
async def agent_loop_with_compression(task: str, agent, compress_every: int = 5):
    working_memory = ""
    history = [{"role": "user", "content": task}]

    for turn in range(30):
        response = await agent.call(history + [
            {"role": "system", "content": f"Working memory:\n{working_memory}"}
        ])
        history.append({"role": "assistant", "content": response})

        # Extract new working memory updates from response
        # (simplified — real implementation would parse structured updates)
        if "[MEMO]" in response:
            new_memo = response.split("[MEMO]")[1].split("[/MEMO]")[0]
            working_memory += f"\n{new_memo}"

        # Compress every N turns
        if turn > 0 and turn % compress_every == 0:
            working_memory = await compress_working_memory(working_memory, agent)

        if "TASK COMPLETE" in response:
            break
```

### Option 4: Structured working memory with expiry

```python
from dataclasses import dataclass, field
from datetime import datetime
import time

@dataclass
class MemoryEntry:
    key: str
    value: str
    created_at: float = field(default_factory=time.monotonic)
    ttl_seconds: float = 300.0  # Entries expire after 5 minutes by default
    is_permanent: bool = False

    def is_expired(self) -> bool:
        if self.is_permanent:
            return False
        return time.monotonic() - self.created_at > self.ttl_seconds

class TtlWorkingMemory:
    """Working memory where entries expire automatically"""

    def __init__(self, max_entries: int = 20):
        self.max_entries = max_entries
        self._store: dict[str, MemoryEntry] = {}

    def set(self, key: str, value: str, ttl: float = 300.0, permanent: bool = False):
        self._evict_expired()
        self._store[key] = MemoryEntry(key, value, ttl_seconds=ttl, is_permanent=permanent)

    def get(self, key: str) -> str | None:
        entry = self._store.get(key)
        if entry and not entry.is_expired():
            return entry.value
        if entry:
            del self._store[key]
        return None

    def _evict_expired(self):
        expired = [k for k, v in self._store.items() if v.is_expired()]
        for k in expired:
            del self._store[k]

        # Also enforce max size — evict oldest
        if len(self._store) > self.max_entries:
            sorted_keys = sorted(self._store, key=lambda k: self._store[k].created_at)
            for k in sorted_keys[:len(self._store) - self.max_entries]:
                if not self._store[k].is_permanent:
                    del self._store[k]

    def as_context(self) -> str:
        self._evict_expired()
        if not self._store:
            return "Working memory: empty"
        entries = "\n".join(
            f"  {k}: {v.value[:100]}"
            for k, v in sorted(self._store.items())
        )
        return f"Working memory ({len(self._store)} entries):\n{entries}"

mem = TtlWorkingMemory()
mem.set("current_step", "analyzing revenue data", ttl=60)    # Expires in 60s
mem.set("task_goal", "generate Q4 report", permanent=True)   # Never expires
mem.set("temp_calc", "1200000 * 0.33", ttl=30)              # Expires in 30s
```

### Option 5: Differentiate conclusion vs scratchpad in system prompt

```
System prompt:
"Working memory discipline:

When working through a task, distinguish between:

CONCLUSIONS (keep permanently):
- Format: CONCLUSION: [key fact or decision]
- These persist in your working memory indefinitely
- Example: CONCLUSION: Database has 45,231 active users as of 2024-01

SCRATCH (discard after use):
- Format: just work through it without marking
- Intermediate calculations, trial approaches, discarded options
- These are NOT recorded and will not appear in later context

CURRENT STEP (replace each turn):
- Format: STEP: [what you're doing now]
- Only one STEP at a time — previous step is automatically replaced

This structure prevents working memory from filling with stale intermediate state.
Only conclusions are retained between steps."
```

## Working Memory Size by Strategy

| Strategy | Memory per 10-step task | Grows with task length? |
|---|---|---|
| All state in context | 20,000+ chars | Yes — unbounded |
| Scratchpad with compression | ~500 chars | No — bounded |
| External store + summary | ~200 chars in context | No — bounded |
| TTL-based expiry | Variable, ~1,000 chars | Partially — auto-expires |
| Conclusions only | ~300 chars | Slow — only finals |

## Expected Token Savings
10-step task with verbose working memory: ~40,000 tokens for state alone
Compressed working memory: ~2,000 tokens for state (95% reduction)

## Environment
- Complex multi-step agent tasks; most impactful for research, analysis, and planning agents
- Source: direct experience; working memory bloat is the second most common cause of context overflow after tool result accumulation
