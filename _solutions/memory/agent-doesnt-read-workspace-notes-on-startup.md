---
layout: solution
title: "Agent Doesn't Load Its Own Notes at Session Start — Redoes Work Already Done"
category: memory
description: "Agent has a workspace notes file from previous sessions but doesn't read it at startup. Starts each session as if no prior work was done, rediscovering and repeating completed work."
tags: [memory, workspace, notes, session-startup, persistence]
---

# Agent Doesn't Load Its Own Notes at Session Start — Redoes Work Already Done

## Symptom
- Agent rediscovers information it already found in previous sessions
- Re-reads the same files, re-runs the same analysis
- Asks the same questions the user already answered
- Session 2 starts as a blank slate despite session 1 completing significant work
- Workspace notes file exists but was never read

## Root Cause
Agents don't automatically read files in their workspace at session start. Without explicit startup instructions to load prior notes, the agent begins with only what's in the system prompt and the new conversation — no memory of previous sessions.

## Fix

### Option 1: Explicit startup instruction to read notes

```
System prompt:
"AT THE START OF EVERY SESSION — before responding to the first user message:
1. Read /workspace/NOTES.md if it exists
2. Read /workspace/session-handoff.md if it exists
3. Briefly summarize what you found (2-3 bullet points)
4. Then address the user's message

If no notes file exists: start fresh and create one as you work."
```

### Option 2: Automate notes loading in agent startup

```python
import os

NOTES_FILES = [
    "/workspace/NOTES.md",
    "/workspace/session-handoff.md",
    "/workspace/.agent-memory.md",
]

def load_session_notes():
    """Load all agent notes at session start"""
    context_additions = []

    for path in NOTES_FILES:
        if os.path.exists(path):
            content = open(path).read()
            if content.strip():
                context_additions.append(f"[Loaded from {path}]:\n{content}")

    return "\n\n".join(context_additions)

# Inject into system prompt at startup
notes = load_session_notes()
SYSTEM_PROMPT = BASE_SYSTEM_PROMPT + (f"\n\n## Prior Session Context\n{notes}" if notes else "")
```

### Option 3: Structured notes format for reliable loading

```markdown
# Agent Notes — /workspace/NOTES.md

## Completed (do not redo)
- [2026-04-15] Analyzed auth module — uses JWT with 1hr expiry
- [2026-04-15] Found the bug: null check missing in line 47 of process_order()
- [2026-04-15] User prefers TypeScript over JavaScript

## In Progress
- Refactoring the payment module (50% done, stopped at checkout.ts line 120)

## User Preferences (confirmed)
- Always use async/await, never callbacks
- Tests required for all new functions
- Never commit directly to main

## Key File Locations
- Config: /workspace/config/openclaw.yaml
- Logs: /workspace/logs/agent.log
- Database schema: /workspace/db/schema.sql
```

### Option 4: Save notes after each session

```python
async def save_session_notes(agent, notes_path="/workspace/NOTES.md"):
    """Ask agent to update notes before session ends"""
    summary = await agent.complete(
        "Update the session notes with what was accomplished today. "
        "Add completed items, update in-progress items, and note any new preferences discovered. "
        f"Current notes:\n{open(notes_path).read() if os.path.exists(notes_path) else '(empty)'}"
    )

    with open(notes_path, 'w') as f:
        f.write(summary)
    print(f"Session notes saved to {notes_path}")

# Call at end of each session or when user says they're done
```

### Option 5: Include notes check in the first message

```python
def build_first_message(user_message):
    """Automatically prepend notes loading request to first message"""
    notes_check = (
        "Before responding: Check if /workspace/NOTES.md exists and read it. "
        "Then address: "
    )
    return notes_check + user_message
```

## Notes File Location Conventions

```bash
# Standard locations for agent notes
/workspace/NOTES.md          # General purpose
/workspace/HANDOFF.md        # Cross-session handoffs
/workspace/.agent-context/   # Directory for multiple notes files
~/.agent-memory/             # Global agent memory (across projects)
```

## Expected Token Savings
Rediscovering completed work across sessions: ~20,000 tokens per session
Reading notes at startup: ~1,000 tokens → saves 19,000 per session

## Environment
- Any long-running agent project with multiple sessions
- Source: direct experience with multi-day agent projects
