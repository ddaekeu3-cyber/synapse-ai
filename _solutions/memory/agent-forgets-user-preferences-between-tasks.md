---
layout: solution
title: "Agent Forgets User Preferences Between Tasks"
category: memory
description: "User sets preferences (language, format, tone, timezone) once, but the agent forgets them on the next task or session — forcing the user to repeat themselves every time."
tags: [memory, preferences, persistence, sqlite, user-experience, personalisation]
---

## Symptom

User says: *"Always respond in French."* The agent does so for the rest of the conversation. Next session: back to English. The user repeats the instruction. Next session: English again.

Or: *"Use bullet points, not paragraphs."* Works for one task, reverts the next.

## Root Cause

User preferences are stored in the conversation's `messages` list, which is ephemeral. When a new session starts, the messages list is empty and no preference context is available. Without an explicit persistence layer, preferences exist only in transient memory.

## Fix

---

### Option 1 — SQLite Preference Store with Auto-Injection

Persist user preferences in SQLite. At the start of every session, load the user's preferences and inject them into the system prompt automatically.

```python
import sqlite3
import json
import re
import anthropic
from pathlib import Path
from datetime import datetime

DB_PATH = Path("user_prefs.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
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
    conn.close()

def get_preferences(user_id: str) -> dict:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT key, value FROM user_preferences WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    conn.close()
    return {row[0]: row[1] for row in rows}

def set_preference(user_id: str, key: str, value: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO user_preferences (user_id, key, value, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
    """, (user_id, key, value, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()
    print(f"[PREF SAVED] {user_id}: {key} = {value!r}")

def build_preference_prompt(prefs: dict) -> str:
    if not prefs:
        return ""
    lines = ["USER PREFERENCES (always apply these):"]
    pref_labels = {
        "language": "Respond in",
        "format": "Format responses as",
        "tone": "Use a",
        "timezone": "Display times in timezone",
        "units": "Use measurement units",
        "length": "Keep responses",
    }
    for key, value in prefs.items():
        label = pref_labels.get(key, key.capitalize())
        lines.append(f"• {label}: {value}")
    return "\n".join(lines)

# Detect preference updates in user messages
PREF_PATTERNS = [
    (re.compile(r"(?:always |please )?respond in (\w+)", re.I), "language"),
    (re.compile(r"use (\w+) format", re.I), "format"),
    (re.compile(r"(?:use a? |be )(\w+) tone", re.I), "tone"),
    (re.compile(r"my timezone is ([A-Z/\w_]+)", re.I), "timezone"),
    (re.compile(r"keep (?:responses? )?(short|brief|concise|detailed|long)", re.I), "length"),
]

def extract_preference_updates(message: str) -> dict:
    updates = {}
    for pattern, key in PREF_PATTERNS:
        match = pattern.search(message)
        if match:
            updates[key] = match.group(1).lower()
    return updates

def chat(user_id: str, messages: list[dict], user_message: str) -> tuple[str, list[dict]]:
    # Detect and save preference updates
    pref_updates = extract_preference_updates(user_message)
    for key, value in pref_updates.items():
        set_preference(user_id, key, value)

    prefs = get_preferences(user_id)
    pref_section = build_preference_prompt(prefs)
    system = "You are a helpful assistant."
    if pref_section:
        system = f"{system}\n\n{pref_section}"

    messages = messages + [{"role": "user", "content": user_message}]

    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=messages,
    )

    reply = response.content[0].text
    messages = messages + [{"role": "assistant", "content": reply}]
    return reply, messages

init_db()

# Session 1
history = []
reply, history = chat("user-alice", history, "Please always respond in French.")
print(f"S1: {reply[:80]}")
reply, history = chat("user-alice", history, "What is the capital of Germany?")
print(f"S1: {reply[:80]}")

# Session 2 — new history, but preferences survive
history2 = []
reply, history2 = chat("user-alice", history2, "What is the capital of Japan?")
print(f"S2 (new session): {reply[:80]}")
# Still responds in French — preference was persisted
```

**Expected Token Savings:** ~20% vs injecting full conversation history; preferences compressed to a few lines
**Environment:** `pip install anthropic`

---

### Option 2 — JSON File Preference Store per User

Store preferences in a per-user JSON file. Simpler than a database for single-process agents. Includes a default preference layer.

```python
import json
import re
import anthropic
from pathlib import Path

PREF_DIR = Path("user_preferences")
PREF_DIR.mkdir(exist_ok=True)

DEFAULT_PREFERENCES = {
    "language": "English",
    "format": "prose",
    "tone": "friendly",
    "length": "concise",
}

def load_preferences(user_id: str) -> dict:
    pref_file = PREF_DIR / f"{user_id}.json"
    if pref_file.exists():
        with open(pref_file) as f:
            stored = json.load(f)
        return {**DEFAULT_PREFERENCES, **stored}
    return DEFAULT_PREFERENCES.copy()

def save_preferences(user_id: str, prefs: dict):
    pref_file = PREF_DIR / f"{user_id}.json"
    # Only save non-default preferences
    to_save = {k: v for k, v in prefs.items() if DEFAULT_PREFERENCES.get(k) != v}
    with open(pref_file, "w") as f:
        json.dump(to_save, f, indent=2)
    print(f"[PREFS] Saved for {user_id}: {to_save}")

def update_prefs_from_message(prefs: dict, message: str) -> tuple[dict, bool]:
    """Parse preference commands from user message. Returns (updated_prefs, changed)."""
    changed = False
    msg_lower = message.lower()

    if "in french" in msg_lower or "respond in french" in msg_lower:
        prefs["language"] = "French"; changed = True
    elif "in spanish" in msg_lower or "en español" in msg_lower:
        prefs["language"] = "Spanish"; changed = True
    elif "in english" in msg_lower:
        prefs["language"] = "English"; changed = True

    if "bullet points" in msg_lower or "use bullets" in msg_lower:
        prefs["format"] = "bullet_points"; changed = True
    elif "use prose" in msg_lower or "paragraphs" in msg_lower:
        prefs["format"] = "prose"; changed = True

    if "formal tone" in msg_lower or "be formal" in msg_lower:
        prefs["tone"] = "formal"; changed = True
    elif "casual" in msg_lower or "informal" in msg_lower:
        prefs["tone"] = "casual"; changed = True

    if "keep it short" in msg_lower or "be brief" in msg_lower:
        prefs["length"] = "very_concise"; changed = True
    elif "detailed" in msg_lower or "thorough" in msg_lower:
        prefs["length"] = "detailed"; changed = True

    return prefs, changed

FORMAT_INSTRUCTIONS = {
    "bullet_points": "Always use bullet points (•) for lists and structure.",
    "prose": "Use flowing prose paragraphs.",
    "numbered": "Use numbered lists for multi-step content.",
}

LENGTH_INSTRUCTIONS = {
    "very_concise": "Keep responses under 50 words.",
    "concise": "Keep responses concise (1-3 sentences for simple questions).",
    "detailed": "Provide thorough, comprehensive responses with examples.",
}

def build_system_prompt(prefs: dict) -> str:
    parts = ["You are a helpful assistant."]

    if prefs.get("language", "English") != "English":
        parts.append(f"Always respond in {prefs['language']}.")

    fmt = FORMAT_INSTRUCTIONS.get(prefs.get("format", "prose"))
    if fmt:
        parts.append(fmt)

    length = LENGTH_INSTRUCTIONS.get(prefs.get("length", "concise"))
    if length:
        parts.append(length)

    if prefs.get("tone") == "formal":
        parts.append("Use a professional, formal tone.")
    elif prefs.get("tone") == "casual":
        parts.append("Use a casual, friendly tone.")

    return " ".join(parts)

client = anthropic.Anthropic()

def chat(user_id: str, history: list[dict], user_message: str) -> tuple[str, list[dict]]:
    prefs = load_preferences(user_id)
    prefs, changed = update_prefs_from_message(prefs, user_message)
    if changed:
        save_preferences(user_id, prefs)

    system = build_system_prompt(prefs)
    messages = history + [{"role": "user", "content": user_message}]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=messages,
    )
    reply = response.content[0].text
    return reply, messages + [{"role": "assistant", "content": reply}]

# Test persistence across sessions
h = []
reply, h = chat("bob", h, "Please use bullet points from now on.")
print(reply[:80])

# New session
h2 = []
reply, h2 = chat("bob", h2, "List 3 benefits of exercise.")
print(reply[:120])
# Uses bullet points — preference was saved
```

**Expected Token Savings:** ~15% — short system prompt section vs long conversation history
**Environment:** `pip install anthropic`

---

### Option 3 — Preference Learning from Implicit Signals

Learn preferences from user behaviour, not just explicit commands. If the user consistently asks follow-up questions like "make it shorter" or "translate to French", detect and persist the implicit preference.

```python
import json
import sqlite3
import anthropic
from pathlib import Path
from collections import Counter

DB_PATH = Path("implicit_prefs.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feedback_signals (
            user_id TEXT,
            signal TEXT,
            count INTEGER DEFAULT 1,
            PRIMARY KEY (user_id, signal)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS learned_preferences (
            user_id TEXT,
            key TEXT,
            value TEXT,
            confidence REAL,
            PRIMARY KEY (user_id, key)
        )
    """)
    conn.commit()
    conn.close()

FEEDBACK_SIGNALS = {
    "too_long": ["make it shorter", "too long", "summarise that", "tldr", "brief version"],
    "too_short": ["more detail", "expand on that", "tell me more", "elaborate"],
    "wrong_language": ["in english please", "en français", "en español", "auf deutsch"],
    "wrong_format": ["use bullet points", "as a list", "in prose", "no bullets"],
    "too_formal": ["less formal", "more casual", "relax the tone"],
    "too_casual": ["more professional", "formal please", "be more formal"],
}

SIGNAL_TO_PREF = {
    "too_long": ("length", "concise"),
    "too_short": ("length", "detailed"),
    "too_formal": ("tone", "casual"),
    "too_casual": ("tone", "formal"),
}

def record_signal(user_id: str, signal: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO feedback_signals (user_id, signal, count)
        VALUES (?, ?, 1)
        ON CONFLICT(user_id, signal) DO UPDATE SET count = count + 1
    """, (user_id, signal))
    conn.commit()
    conn.close()

def detect_signals(message: str) -> list[str]:
    msg_lower = message.lower()
    detected = []
    for signal, phrases in FEEDBACK_SIGNALS.items():
        if any(phrase in msg_lower for phrase in phrases):
            detected.append(signal)
    return detected

def update_learned_preferences(user_id: str, threshold: int = 2):
    """Promote signals that have appeared >= threshold times to learned preferences."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT signal, count FROM feedback_signals WHERE user_id = ? AND count >= ?",
        (user_id, threshold),
    ).fetchall()

    for signal, count in rows:
        pref = SIGNAL_TO_PREF.get(signal)
        if pref:
            key, value = pref
            confidence = min(count / 5.0, 1.0)
            conn.execute("""
                INSERT INTO learned_preferences (user_id, key, value, confidence)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, confidence=excluded.confidence
            """, (user_id, key, value, confidence))

    conn.commit()
    conn.close()

def get_learned_preferences(user_id: str) -> dict:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT key, value, confidence FROM learned_preferences WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    conn.close()
    return {row[0]: {"value": row[1], "confidence": row[2]} for row in rows}

client = anthropic.Anthropic()
init_db()

def chat(user_id: str, history: list[dict], user_message: str) -> tuple[str, list[dict]]:
    # Detect and record signals
    signals = detect_signals(user_message)
    for signal in signals:
        record_signal(user_id, signal)
        print(f"[SIGNAL] {signal} recorded for {user_id}")

    update_learned_preferences(user_id)

    # Build system prompt from learned preferences
    learned = get_learned_preferences(user_id)
    pref_lines = []
    for key, pref_data in learned.items():
        if pref_data["confidence"] >= 0.4:
            pref_lines.append(f"• {key}: {pref_data['value']} (confidence: {pref_data['confidence']:.0%})")

    system = "You are a helpful assistant."
    if pref_lines:
        system += "\n\nLEARNED USER PREFERENCES:\n" + "\n".join(pref_lines)

    messages = history + [{"role": "user", "content": user_message}]
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=messages,
    )
    reply = response.content[0].text
    return reply, messages + [{"role": "assistant", "content": reply}]

h = []
for msg in [
    "Explain machine learning.",
    "Make it shorter please.",
    "More concise.",
    "Brief version only.",
    "What is deep learning?",  # Should auto-apply concise preference
]:
    reply, h = chat("carol", h, msg)
    print(f"Q: {msg[:40]}")
    print(f"A: {reply[:80]}\n")
```

**Expected Token Savings:** None — UX improvement; reduces repeat correction turns
**Environment:** `pip install anthropic`

---

### Option 4 — Preference Profile with Versioning

Store preferences with version history. Allow users to reset to defaults or view their current profile. Supports preference profiles (e.g. "work mode" vs "casual mode").

```python
import json
import sqlite3
import anthropic
from pathlib import Path
from datetime import datetime

DB_PATH = Path("preference_profiles.db")

PROFILES = {
    "work": {
        "language": "English",
        "tone": "formal",
        "format": "structured",
        "length": "detailed",
    },
    "casual": {
        "language": "English",
        "tone": "friendly",
        "format": "prose",
        "length": "concise",
    },
    "default": {
        "language": "English",
        "tone": "helpful",
        "format": "prose",
        "length": "balanced",
    },
}

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id TEXT,
            profile_name TEXT DEFAULT 'default',
            preferences TEXT NOT NULL,
            updated_at TEXT,
            PRIMARY KEY (user_id)
        )
    """)
    conn.commit()
    conn.close()

def get_user_profile(user_id: str) -> tuple[str, dict]:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT profile_name, preferences FROM user_profiles WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    conn.close()

    if row:
        return row[0], json.loads(row[1])
    return "default", PROFILES["default"].copy()

def save_user_profile(user_id: str, profile_name: str, preferences: dict):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO user_profiles (user_id, profile_name, preferences, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            profile_name=excluded.profile_name,
            preferences=excluded.preferences,
            updated_at=excluded.updated_at
    """, (user_id, profile_name, json.dumps(preferences), datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

def switch_profile(user_id: str, profile_name: str) -> str:
    if profile_name not in PROFILES:
        return f"Unknown profile: {profile_name}. Available: {list(PROFILES.keys())}"
    save_user_profile(user_id, profile_name, PROFILES[profile_name].copy())
    return f"Switched to '{profile_name}' profile: {PROFILES[profile_name]}"

def update_single_preference(user_id: str, key: str, value: str) -> str:
    profile_name, prefs = get_user_profile(user_id)
    prefs[key] = value
    save_user_profile(user_id, profile_name, prefs)
    return f"Updated {key} = {value!r}"

def handle_preference_command(user_id: str, message: str) -> str | None:
    msg_lower = message.lower().strip()
    if msg_lower.startswith("switch to ") and "profile" in msg_lower:
        profile = msg_lower.replace("switch to ", "").replace(" profile", "").strip()
        return switch_profile(user_id, profile)
    if msg_lower in ("show my preferences", "my settings", "what are my preferences"):
        _, prefs = get_user_profile(user_id)
        return f"Your preferences:\n" + "\n".join(f"• {k}: {v}" for k, v in prefs.items())
    if msg_lower in ("reset preferences", "reset to defaults"):
        save_user_profile(user_id, "default", PROFILES["default"].copy())
        return "Preferences reset to defaults."
    return None

client = anthropic.Anthropic()
init_db()

def chat(user_id: str, history: list[dict], user_message: str) -> tuple[str, list[dict]]:
    # Handle meta-commands
    command_result = handle_preference_command(user_id, user_message)
    if command_result:
        return command_result, history

    profile_name, prefs = get_user_profile(user_id)
    system = (
        f"You are a helpful assistant.\n\n"
        f"Active profile: {profile_name}\n"
        f"Language: {prefs.get('language', 'English')}\n"
        f"Tone: {prefs.get('tone', 'helpful')}\n"
        f"Format: {prefs.get('format', 'prose')}\n"
        f"Length: {prefs.get('length', 'balanced')}"
    )

    messages = history + [{"role": "user", "content": user_message}]
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=messages,
    )
    reply = response.content[0].text
    return reply, messages + [{"role": "assistant", "content": reply}]

h = []
reply, h = chat("dave", h, "switch to work profile")
print(reply)
reply, h = chat("dave", h, "Explain quantum computing briefly.")
print(reply[:120])
reply, h = chat("dave", h, "show my preferences")
print(reply)
```

**Expected Token Savings:** None — UX improvement; reduces per-session preference repetition
**Environment:** `pip install anthropic`

---

### Option 5 — Cached Preference System Prompt

Use prompt caching to cache the user's preference system prompt. When preferences don't change, subsequent requests in the session pay zero input tokens for the preference block.

```python
import json
import sqlite3
import anthropic
from pathlib import Path

DB_PATH = Path("cached_prefs.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prefs (
            user_id TEXT PRIMARY KEY,
            prefs_json TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def get_prefs(user_id: str) -> dict:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT prefs_json FROM prefs WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return json.loads(row[0]) if row else {}

def set_prefs(user_id: str, prefs: dict):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO prefs (user_id, prefs_json) VALUES (?, ?)",
        (user_id, json.dumps(prefs)),
    )
    conn.commit()
    conn.close()

def build_pref_text(prefs: dict) -> str:
    if not prefs:
        return "No special preferences set."
    lines = ["USER PREFERENCES — apply to all responses:"]
    for k, v in prefs.items():
        lines.append(f"  {k}: {v}")
    return "\n".join(lines)

client = anthropic.Anthropic()
init_db()

def chat_with_cached_prefs(
    user_id: str,
    history: list[dict],
    user_message: str,
) -> tuple[str, list[dict]]:
    prefs = get_prefs(user_id)
    pref_text = build_pref_text(prefs)

    # Cache the preference block — 0 cost on repeat calls within session
    system = [
        {
            "type": "text",
            "text": f"You are a helpful assistant.\n\n{pref_text}",
            "cache_control": {"type": "ephemeral"},
        }
    ]

    messages = history + [{"role": "user", "content": user_message}]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=messages,
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )

    reply = response.content[0].text
    usage = response.usage
    cache_hit = getattr(usage, "cache_read_input_tokens", 0) > 0
    print(f"[Cache {'HIT' if cache_hit else 'MISS'}] pref block")

    return reply, messages + [{"role": "assistant", "content": reply}]

# Setup preferences
set_prefs("eve", {"language": "French", "format": "bullet_points", "length": "concise"})

h = []
for q in ["What is Python?", "What is asyncio?", "What is Pydantic?"]:
    reply, h = chat_with_cached_prefs("eve", h, q)
    print(f"Q: {q}")
    print(f"A: {reply[:80]}\n")
# After first call: pref block is cached — subsequent calls pay 0 tokens for it
```

**Expected Token Savings:** ~60% on preference prompt tokens after first call in session
**Environment:** `pip install anthropic`

---

### Option 6 — Preference Extraction Tool for Self-Updating Memory

Give the agent a `save_preference` tool. When the agent detects a user stating a preference, it calls the tool automatically — no manual parsing needed.

```python
import json
import sqlite3
import anthropic
from pathlib import Path

DB_PATH = Path("tool_prefs.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS preferences (
            user_id TEXT,
            key TEXT,
            value TEXT,
            PRIMARY KEY (user_id, key)
        )
    """)
    conn.commit()
    conn.close()

def save_preference_to_db(user_id: str, key: str, value: str) -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT OR REPLACE INTO preferences (user_id, key, value) VALUES (?, ?, ?)
    """, (user_id, key, value))
    conn.commit()
    conn.close()
    print(f"[TOOL] Saved preference: {user_id} → {key}={value!r}")
    return {"saved": True, "key": key, "value": value}

def get_all_preferences(user_id: str) -> dict:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT key, value FROM preferences WHERE user_id = ?", (user_id,)).fetchall()
    conn.close()
    return {row[0]: row[1] for row in rows}

client = anthropic.Anthropic()
init_db()

TOOLS = [{
    "name": "save_preference",
    "description": (
        "Save a user preference for future sessions. "
        "Call this whenever the user states a preference about how they want responses. "
        "Examples: language, format (bullets/prose), tone, response length."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Preference name (e.g. 'language', 'format', 'tone', 'length')",
            },
            "value": {
                "type": "string",
                "description": "Preference value (e.g. 'French', 'bullet_points', 'formal', 'concise')",
            },
        },
        "required": ["key", "value"],
        "additionalProperties": False,
    },
}]

def chat(user_id: str, history: list[dict], user_message: str) -> tuple[str, list[dict]]:
    prefs = get_all_preferences(user_id)
    pref_note = ""
    if prefs:
        pref_note = "\n\nCurrent user preferences: " + ", ".join(f"{k}={v}" for k, v in prefs.items())

    system = (
        "You are a helpful assistant. "
        "When a user states a preference, call save_preference immediately before responding."
        + pref_note
    )

    messages = history + [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            reply = next((b.text for b in response.content if b.type == "text"), "")
            messages.append({"role": "assistant", "content": reply})
            return reply, messages

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use" and block.name == "save_preference":
                result = save_preference_to_db(user_id, **block.input)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
        if tool_results:
            messages.append({"role": "user", "content": tool_results})

h = []
reply, h = chat("frank", h, "Please always respond in Spanish from now on.")
print(reply[:80])

# New session — preferences loaded from DB automatically
h2 = []
reply, h2 = chat("frank", h2, "What is machine learning?")
print(reply[:80])
# Responds in Spanish — preference was saved by the tool
```

**Expected Token Savings:** None — eliminates manual parsing; agent self-updates preference store
**Environment:** `pip install anthropic`

---

## Comparison

| Option | Storage | Auto-Detect | Cross-Session | Best For |
|--------|---------|-------------|---------------|----------|
| SQLite Store + Regex | SQLite | Yes (regex) | Yes | Production single-server agents |
| JSON File Store | JSON files | Yes (regex) | Yes | Simple single-process agents |
| Implicit Signal Learning | SQLite | Yes (implicit) | Yes | Long-running consumer bots |
| Profile System | SQLite | No (explicit) | Yes | Power users with multiple modes |
| Cached Pref Prompt | SQLite + cache | No | Yes | High-throughput production |
| Save Preference Tool | SQLite | Yes (via LLM) | Yes | Agents with tool use |

**Recommended starting point:** Option 1 (SQLite + Regex) for most agents. Add Option 5 (Cached Pref Prompt) in production for token savings.
