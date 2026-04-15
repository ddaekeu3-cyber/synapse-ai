---
layout: solution
title: "Agent Forgets Tool Call Results After Context Compression"
category: memory
description: "Agent reads a file or queries a database early in a long session. After many turns, context is compressed and the tool result is summarized away. Agent now acts as if it never read the file."
tags: [memory, context-compression, tool-results, sliding-window, session, retrieval]
---

# Agent Forgets Tool Call Results After Context Compression

## Symptom
- Agent read a config file at turn 5; at turn 40 it asks to read it again
- Database query result referenced correctly early in session, forgotten later
- Agent says "I don't have that information" about data it retrieved 20 turns ago
- Context compression summarized "the agent read file X and found Y" → detail Y is lost
- Agent re-runs expensive tool calls unnecessarily

## Root Cause
Context compression strategies (sliding window, summarization) prioritize recent turns and condense older ones. Tool results — even critical data retrieved from files or APIs — get compressed into vague summaries that lose the actual data values. The agent then lacks the specific values it previously retrieved.

## Fix

### Option 1: Maintain a persistent tool result cache alongside history

```python
from datetime import datetime
from typing import Any

class AgentWithToolMemory:
    def __init__(self):
        self.history = []
        self.tool_cache = {}  # key -> {result, retrieved_at, tool_name}

    def cache_tool_result(self, key: str, result: Any, tool_name: str):
        self.tool_cache[key] = {
            "result": result,
            "tool_name": tool_name,
            "retrieved_at": datetime.utcnow().isoformat()
        }

    def get_cached_result(self, key: str) -> Any | None:
        entry = self.tool_cache.get(key)
        return entry["result"] if entry else None

    def build_tool_summary_for_context(self) -> str:
        """Inject cached results into every API call as a stable section"""
        if not self.tool_cache:
            return ""
        lines = ["## Retrieved Data (always available, not affected by context limits):"]
        for key, entry in self.tool_cache.items():
            lines.append(f"### {key} (from {entry['tool_name']} at {entry['retrieved_at']})")
            lines.append(str(entry["result"])[:2000])  # Limit per entry
        return "\n".join(lines)

    async def complete(self, user_message: str, agent) -> str:
        tool_summary = self.build_tool_summary_for_context()
        system_injection = f"\n\n{tool_summary}" if tool_summary else ""

        response = await agent.complete(
            system=BASE_SYSTEM + system_injection,
            messages=self.compress_history() + [{"role": "user", "content": user_message}]
        )
        return response
```

### Option 2: Pin critical tool results as permanent context

```python
class PinnedContextManager:
    def __init__(self):
        self.pinned = []  # Critical data that survives compression
        self.history = []

    def pin(self, label: str, content: str):
        """Pin important tool result — never compressed away"""
        self.pinned.append({"label": label, "content": content})

    def build_messages(self, new_user_message: str) -> list:
        """Always include pinned content before history"""
        pinned_section = ""
        if self.pinned:
            pinned_section = "## Pinned Data (always in context):\n"
            for item in self.pinned:
                pinned_section += f"\n### {item['label']}\n{item['content']}\n"

        # Compress history but keep pinned data
        compressed_history = self.compress_old_history(self.history)

        return [
            {
                "role": "user",
                "content": pinned_section + "\n---\n" + compressed_history[0]["content"]
                if compressed_history else pinned_section
            },
            *compressed_history[1:],
            {"role": "user", "content": new_user_message}
        ]

# Usage
ctx = PinnedContextManager()

# After reading config file
config_content = read_file_tool("config.yaml")
ctx.pin("config.yaml contents", config_content)  # Never compressed
```

### Option 3: Structured workspace notes file

```python
from pathlib import Path
import json

WORKSPACE_NOTES = Path(".agent_workspace/session_notes.json")

def save_tool_result_to_workspace(key: str, data: Any, description: str):
    """Persist tool results outside the context window"""
    WORKSPACE_NOTES.parent.mkdir(exist_ok=True)
    notes = json.loads(WORKSPACE_NOTES.read_text()) if WORKSPACE_NOTES.exists() else {}
    notes[key] = {
        "data": data,
        "description": description,
        "saved_at": datetime.utcnow().isoformat()
    }
    WORKSPACE_NOTES.write_text(json.dumps(notes, indent=2))

def get_workspace_summary() -> str:
    """Include in system prompt so agent always knows what's been retrieved"""
    if not WORKSPACE_NOTES.exists():
        return ""
    notes = json.loads(WORKSPACE_NOTES.read_text())
    lines = ["Previously retrieved data (use this, don't re-fetch):"]
    for key, entry in notes.items():
        lines.append(f"- {key}: {entry['description']}")
    return "\n".join(lines)

# System prompt includes workspace summary
system = f"{BASE_SYSTEM}\n\n{get_workspace_summary()}"
```

### Option 4: Detect when agent tries to re-fetch cached data

```python
RE_FETCH_PATTERNS = [
    "let me read", "I'll check the file", "let me look at",
    "I need to fetch", "let me retrieve", "let me query",
    "can you share", "I don't have access to"
]

def detect_unnecessary_refetch(agent_response: str, tool_cache: dict) -> bool:
    """Detect if agent is about to re-fetch something already in cache"""
    response_lower = agent_response.lower()
    if not any(p in response_lower for p in RE_FETCH_PATTERNS):
        return False

    # Check if any cached item is referenced
    for key in tool_cache:
        if key.lower() in response_lower:
            return True

    return False

async def run_with_refetch_prevention(user_message: str, agent, cache: dict) -> str:
    response = await agent.complete(user_message)

    if detect_unnecessary_refetch(response, cache):
        # Inject cached data and remind agent
        cached_summary = format_cache_for_agent(cache)
        reminder = f"You already have this data in your context:\n{cached_summary}\nPlease use the cached data rather than re-fetching."
        response = await agent.complete(reminder)

    return response
```

### Option 5: Tool call deduplication

```python
import hashlib, json

class DeduplicatingToolWrapper:
    def __init__(self):
        self._cache = {}

    def _cache_key(self, tool_name: str, tool_input: dict) -> str:
        content = json.dumps({"tool": tool_name, "input": tool_input}, sort_keys=True)
        return hashlib.md5(content.encode()).hexdigest()

    async def call(self, tool_name: str, tool_input: dict) -> dict:
        key = self._cache_key(tool_name, tool_input)

        if key in self._cache:
            print(f"Cache hit for {tool_name}({tool_input}) — skipping re-fetch")
            return self._cache[key]

        result = await execute_tool(tool_name, tool_input)
        self._cache[key] = result
        return result

tools = DeduplicatingToolWrapper()
# Second call to read same file returns cached result instantly
```

## What Gets Lost in Context Compression

| Content | Survives compression? | Fix |
|---|---|---|
| Recent conversation turns | Yes | No action needed |
| Old file contents (verbatim) | No — summarized | Pin or cache separately |
| Database query results | No — summarized | Cache in workspace notes |
| Error messages from old turns | Partially | Pin if critical |
| User preferences set early | No — lost | Save to preferences file |
| API response data | No — summarized | Cache in tool memory |

## Expected Token Savings
Re-running expensive tool calls: ~5,000 tokens per re-fetch
Tool result cache prevents redundant calls: 0 extra tokens

## Environment
- Long-running agents with many tool calls; most critical for file/DB-heavy workflows
- Source: direct experience with agents losing context of retrieved data
