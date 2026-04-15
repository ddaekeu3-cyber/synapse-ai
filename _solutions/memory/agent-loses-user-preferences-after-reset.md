---
layout: solution
title: "Agent Loses User Preferences After Session Reset — Settings Not Persisted"
category: memory
description: "User sets preferences (language, tone, output format) in session. After session reset or restart, agent reverts to defaults and asks the same setup questions again."
tags: [memory, preferences, persistence, session, user-settings]
---

# Agent Loses User Preferences After Session Reset — Settings Not Persisted

## Symptom
- User says "always respond in bullet points" — agent forgets after restart
- Language preference (e.g., "respond in Korean") lost between sessions
- Agent asks "how verbose should I be?" again even though user answered last week
- Custom persona or tone resets to default
- User has to re-configure the agent on every session start

## Root Cause
LLM sessions are stateless by default. Preferences set in conversation history are lost when history is cleared. There is no built-in persistence layer — the agent must explicitly save and load user preferences from an external store.

## Fix

### Option 1: Save preferences to a file, load on startup

```python
import json, os
from pathlib import Path

PREFS_FILE = Path.home() / ".agent_preferences.json"

def load_preferences() -> dict:
    if PREFS_FILE.exists():
        return json.loads(PREFS_FILE.read_text())
    return {}

def save_preferences(prefs: dict):
    PREFS_FILE.write_text(json.dumps(prefs, indent=2))

def preferences_to_system_prompt(prefs: dict) -> str:
    if not prefs:
        return ""
    lines = ["User preferences (always apply these):"]
    for key, value in prefs.items():
        lines.append(f"- {key}: {value}")
    return "\n".join(lines)

# On startup: inject preferences into system prompt
prefs = load_preferences()
system_prompt = BASE_SYSTEM_PROMPT + "\n\n" + preferences_to_system_prompt(prefs)
```

### Option 2: Detect and extract preferences from conversation

```python
PREFERENCE_KEYWORDS = [
    "always", "never", "prefer", "don't", "please use",
    "I like", "from now on", "every time", "remember that"
]

async def extract_and_save_preferences(user_message: str, agent):
    """Detect if user is expressing a preference and save it"""
    is_preference = any(kw in user_message.lower() for kw in PREFERENCE_KEYWORDS)
    if not is_preference:
        return

    # Ask agent to extract structured preference
    extraction = await agent.complete([{
        "role": "user",
        "content": f"""Extract the user preference from this message as JSON:
Message: "{user_message}"
Format: {{"key": "preference_name", "value": "preference_value"}}
If no clear preference, return {{"key": null}}"""
    }])

    result = json.loads(extraction)
    if result.get("key"):
        prefs = load_preferences()
        prefs[result["key"]] = result["value"]
        save_preferences(prefs)
        print(f"Saved preference: {result['key']} = {result['value']}")
```

### Option 3: Explicit preference commands

```python
# Support slash commands for preference management
def handle_preference_command(command: str) -> str:
    """
    /set language=Korean
    /set tone=concise
    /set format=bullet-points
    /prefs — show current preferences
    /reset-prefs — clear all preferences
    """
    if command.startswith("/set "):
        key, _, value = command[5:].partition("=")
        prefs = load_preferences()
        prefs[key.strip()] = value.strip()
        save_preferences(prefs)
        return f"Preference saved: {key.strip()} = {value.strip()}"

    elif command == "/prefs":
        prefs = load_preferences()
        if not prefs:
            return "No preferences saved."
        return "\n".join(f"  {k}: {v}" for k, v in prefs.items())

    elif command == "/reset-prefs":
        save_preferences({})
        return "All preferences cleared."
```

### Option 4: Database-backed preferences (multi-user)

```python
import sqlite3
from datetime import datetime

def init_prefs_db(db_path="agent_prefs.db"):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_preferences (
            user_id TEXT,
            key TEXT,
            value TEXT,
            updated_at TEXT,
            PRIMARY KEY (user_id, key)
        )
    """)
    conn.commit()
    return conn

def get_user_prefs(conn, user_id: str) -> dict:
    rows = conn.execute(
        "SELECT key, value FROM user_preferences WHERE user_id = ?",
        (user_id,)
    ).fetchall()
    return {row[0]: row[1] for row in rows}

def set_user_pref(conn, user_id: str, key: str, value: str):
    conn.execute("""
        INSERT OR REPLACE INTO user_preferences (user_id, key, value, updated_at)
        VALUES (?, ?, ?, ?)
    """, (user_id, key, value, datetime.utcnow().isoformat()))
    conn.commit()
```

### Option 5: Agent workspace notes (file-based, agent-readable)

```markdown
<!-- ~/.agent_workspace/PREFERENCES.md -->
# User Preferences

- **Language**: Always respond in English
- **Format**: Use bullet points for lists, code blocks for code
- **Verbosity**: Concise — no lengthy explanations unless asked
- **Tone**: Professional but direct
- **Auto-save**: Don't ask "shall I save this?" — just save
```

```python
PREFS_FILE = Path.home() / ".agent_workspace/PREFERENCES.md"

system_prompt = f"""
{BASE_SYSTEM_PROMPT}

At the start of every session, read and apply the user's saved preferences:
{PREFS_FILE.read_text() if PREFS_FILE.exists() else "No preferences file found."}
"""
```

## Common Preferences Worth Persisting

| Preference | Example Value |
|------------|--------------|
| Language | "Always respond in Korean" |
| Format | "Use markdown with headers" |
| Verbosity | "Be concise, skip explanations" |
| Persona | "You are a senior Python engineer" |
| Timezone | "UTC+9 (Seoul)" |
| Code style | "Use type hints, no comments" |
| Confirmation | "Don't ask to confirm — just do it" |

## Expected Token Savings
Re-explaining preferences every session: ~2,000 tokens/session
Loaded from file at startup: ~200 tokens (one-time read)

## Environment
- Any agent with persistent user sessions
- Source: direct experience, common request in long-term agent deployments
