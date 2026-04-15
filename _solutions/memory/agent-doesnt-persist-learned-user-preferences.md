---
layout: solution
title: "Agent Doesn't Persist Learned User Preferences"
category: memory
description: "The agent learns facts about users during conversation but forgets them on restart. Users repeat themselves every session, trust erodes, and personalization is impossible."
tags: [memory, personalization, persistence, sqlite, user-preferences, extraction]
---

## Symptom

A user tells the agent "I prefer concise answers in Python, not JavaScript." The agent respects this for the current session. Next session: same generic defaults. The user explains again. This repeats indefinitely. Long-term users get the same experience as first-time users regardless of how much context they have provided.

## Root Cause

Preference learning happens implicitly in the conversation history, which is ephemeral by design. When the session ends or the context is cleared, all learned preferences are discarded. There is no extraction step that identifies durable facts from transient conversation content, and no persistence layer that survives process restarts.

## Fix

---

### Option 1: SQLite Preference Store with Automatic Extraction

At the end of each response, run a cheap Haiku extraction pass to identify new preferences. Persist them to SQLite. Inject on next session.

```python
import json
import sqlite3
import anthropic
from datetime import datetime, timezone

client = anthropic.Anthropic()
DB_PATH = "user_preferences.db"


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS preferences (
            user_id   TEXT,
            key       TEXT,
            value     TEXT,
            updated   TEXT,
            PRIMARY KEY (user_id, key)
        )
    """)
    conn.commit()
    return conn


conn = init_db()


def load_preferences(user_id: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT key, value FROM preferences WHERE user_id = ?", (user_id,)
    ).fetchall()
    return {k: v for k, v in rows}


def save_preferences(user_id: str, prefs: dict[str, str]):
    now = datetime.now(timezone.utc).isoformat()
    for key, value in prefs.items():
        conn.execute(
            """INSERT INTO preferences (user_id, key, value, updated) VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, updated=excluded.updated""",
            (user_id, key, value, now),
        )
    conn.commit()


EXTRACT_PROMPT = """
Analyze the user's message and extract any stated preferences, constraints, or personal facts.
Return a JSON object with preference keys and values.
Only include things explicitly stated by the user, not inferred.
If no preferences are stated, return {{}}.

Examples of things to extract:
- language preference: "I prefer Python" → {{"preferred_language": "Python"}}
- verbosity: "keep it brief" → {{"response_style": "concise"}}
- expertise: "I'm a beginner" → {{"expertise_level": "beginner"}}
- format: "use bullet points" → {{"output_format": "bullet_points"}}
- domain: "I work in finance" → {{"domain": "finance"}}

User message: {message}
"""


def extract_preferences(message: str) -> dict[str, str]:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": EXTRACT_PROMPT.format(message=message),
        }],
    )
    text = response.content[0].text.strip()
    try:
        # Extract JSON from response
        start = text.find("{")
        end = text.rfind("}") + 1
        return json.loads(text[start:end]) if start != -1 else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def build_preference_context(prefs: dict[str, str]) -> str:
    if not prefs:
        return ""
    lines = [f"- {k.replace('_', ' ').title()}: {v}" for k, v in prefs.items()]
    return "User preferences from previous sessions:\n" + "\n".join(lines)


class PersonalizedAgent:
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.preferences = load_preferences(user_id)
        self._history: list[dict] = []
        print(f"Loaded {len(self.preferences)} preferences for {user_id}")

    def chat(self, user_message: str) -> str:
        # Extract and persist any new preferences
        new_prefs = extract_preferences(user_message)
        if new_prefs:
            self.preferences.update(new_prefs)
            save_preferences(self.user_id, new_prefs)
            print(f"  [Saved preferences: {new_prefs}]")

        # Build system with preference context
        pref_context = build_preference_context(self.preferences)
        system = "You are a helpful assistant.\n\n" + pref_context if pref_context else "You are a helpful assistant."

        self._history.append({"role": "user", "content": user_message})
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            messages=self._history,
        )
        reply = response.content[0].text
        self._history.append({"role": "assistant", "content": reply})
        return reply


# Session 1: user states preferences
print("=== Session 1 ===")
agent = PersonalizedAgent("user_alice")
print(agent.chat("I prefer concise answers and always use Python, not JavaScript."))
print(agent.chat("How do I read a file?"))

# Session 2: preferences are automatically loaded
print("\n=== Session 2 (new session, same user) ===")
agent2 = PersonalizedAgent("user_alice")  # reloads from DB
print(agent2.chat("How do I make an HTTP request?"))
```

**Expected Token Savings**: Preference context adds ~50 tokens/request but eliminates repetitive clarification turns that cost 200+ tokens each.
**Environment**: SQLite, no external dependencies. Survives restarts.

---

### Option 2: Structured Preference Schema with Explicit User Control

Use a fixed preference schema. Let users view, edit, and delete their preferences explicitly.

```python
import json
import anthropic
from pathlib import Path
from dataclasses import dataclass, asdict, field

@dataclass
class UserPreferences:
    user_id: str
    language: str = "python"
    verbosity: str = "balanced"         # concise | balanced | detailed
    expertise_level: str = "intermediate"  # beginner | intermediate | expert
    output_format: str = "prose"        # prose | bullets | code_first
    domain: str = ""
    name: str = ""
    timezone: str = "UTC"
    custom: dict = field(default_factory=dict)


PREF_DIR = Path("preferences")
PREF_DIR.mkdir(exist_ok=True)

client = anthropic.Anthropic()


def load(user_id: str) -> UserPreferences:
    path = PREF_DIR / f"{user_id}.json"
    if path.exists():
        data = json.loads(path.read_text())
        return UserPreferences(**data)
    return UserPreferences(user_id=user_id)


def save(prefs: UserPreferences):
    path = PREF_DIR / f"{prefs.user_id}.json"
    path.write_text(json.dumps(asdict(prefs), indent=2))


PREFERENCE_UPDATE_TOOL = {
    "name": "update_user_preference",
    "description": "Update a user preference when the user explicitly states one.",
    "input_schema": {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "enum": ["language", "verbosity", "expertise_level", "output_format", "domain", "name", "timezone"],
                "description": "The preference to update",
            },
            "value": {"type": "string", "description": "The new value"},
            "reason": {"type": "string", "description": "Why this preference was inferred"},
        },
        "required": ["key", "value", "reason"],
    },
}


def build_system(prefs: UserPreferences) -> str:
    return f"""You are a helpful assistant. Adapt your responses to the user's preferences.

Current user preferences:
- Name: {prefs.name or "unknown"}
- Language: {prefs.language}
- Verbosity: {prefs.verbosity}
- Expertise: {prefs.expertise_level}
- Format: {prefs.output_format}
- Domain: {prefs.domain or "general"}

If the user states a new preference, use the update_user_preference tool to record it.
"""


def chat_with_preference_learning(user_id: str, message: str) -> str:
    prefs = load(user_id)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=build_system(prefs),
        tools=[PREFERENCE_UPDATE_TOOL],
        messages=[{"role": "user", "content": message}],
    )

    # Process tool calls
    updates = {}
    text_parts = []
    for block in response.content:
        if block.type == "tool_use" and block.name == "update_user_preference":
            key = block.input["key"]
            value = block.input["value"]
            setattr(prefs, key, value)
            updates[key] = value
            print(f"  [Preference updated: {key} = {value} ({block.input['reason']})]")
        elif block.type == "text":
            text_parts.append(block.text)

    if updates:
        save(prefs)

    # If tool was used, get final text response
    if response.stop_reason == "tool_use":
        tool_results = [
            {"type": "tool_result", "tool_use_id": b.id, "content": "Preference saved."}
            for b in response.content if b.type == "tool_use"
        ]
        final = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=build_system(prefs),
            tools=[PREFERENCE_UPDATE_TOOL],
            messages=[
                {"role": "user", "content": message},
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": tool_results},
            ],
        )
        return final.content[0].text

    return "\n".join(text_parts)


# Usage
print(chat_with_preference_learning("alice", "I'm an expert Python developer. Please be concise."))
print(chat_with_preference_learning("alice", "How do I sort a list of dicts by a key?"))
```

**Expected Token Savings**: Structured schema prevents ambiguous preferences; tool-based extraction is more accurate than free-form parsing.
**Environment**: File-based JSON storage. Easy to inspect and debug.

---

### Option 3: Preference Embedding Search for Similar Users

Build a preference profile and find similar users to bootstrap cold-start personalization.

```python
import json
import numpy as np
import anthropic
from pathlib import Path

client = anthropic.Anthropic()
PROFILE_DIR = Path("user_profiles")
PROFILE_DIR.mkdir(exist_ok=True)


def hash_embed(text: str, dim: int = 256) -> list[float]:
    """Deterministic hash-based pseudo-embedding for demo."""
    vec = np.zeros(dim)
    for i, char in enumerate(text):
        vec[hash(char + str(i)) % dim] += 1.0
    norm = np.linalg.norm(vec)
    return (vec / norm).tolist() if norm > 0 else vec.tolist()


def save_profile(user_id: str, preferences: dict, conversation_sample: str):
    profile_text = json.dumps(preferences) + " " + conversation_sample
    profile = {
        "user_id": user_id,
        "preferences": preferences,
        "embedding": hash_embed(profile_text),
    }
    (PROFILE_DIR / f"{user_id}.json").write_text(json.dumps(profile))


def find_similar_users(new_user_prefs: dict, top_k: int = 3) -> list[dict]:
    """Find existing users with similar preference profiles."""
    if not new_user_prefs:
        return []

    query_vec = np.array(hash_embed(json.dumps(new_user_prefs)))
    scored = []

    for profile_file in PROFILE_DIR.glob("*.json"):
        profile = json.loads(profile_file.read_text())
        other_vec = np.array(profile["embedding"])
        sim = float(np.dot(query_vec, other_vec))
        scored.append((sim, profile))

    scored.sort(key=lambda x: -x[0])
    return [p for _, p in scored[:top_k]]


def bootstrap_preferences_from_similar(new_prefs: dict) -> dict:
    """Infer missing preferences from similar users."""
    similar = find_similar_users(new_prefs)
    if not similar:
        return new_prefs

    merged = dict(new_prefs)
    for profile in similar:
        for k, v in profile["preferences"].items():
            if k not in merged:
                merged[k] = v  # fill missing fields from similar user

    print(f"  [Bootstrapped {len(merged) - len(new_prefs)} preferences from {len(similar)} similar users]")
    return merged


# Seed with some existing user profiles
save_profile("user_001", {
    "language": "python", "verbosity": "concise", "expertise": "expert",
    "domain": "data science"
}, "I use pandas and numpy daily. Keep code examples short.")

save_profile("user_002", {
    "language": "python", "verbosity": "detailed", "expertise": "beginner",
    "domain": "web development"
}, "I'm learning FastAPI. Please explain everything step by step.")


def smart_onboarding_chat(user_id: str, message: str) -> str:
    # For new users, extract initial prefs and bootstrap from similar users
    profile_path = PROFILE_DIR / f"{user_id}.json"
    if profile_path.exists():
        prefs = json.loads(profile_path.read_text())["preferences"]
    else:
        # Extract initial preferences from first message
        extract_response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content":
                f"Extract user preferences as JSON from: '{message}'. Return only valid JSON."}],
        )
        try:
            text = extract_response.content[0].text
            prefs = json.loads(text[text.find("{"):text.rfind("}")+1])
        except Exception:
            prefs = {}

        # Bootstrap from similar users
        prefs = bootstrap_preferences_from_similar(prefs)
        save_profile(user_id, prefs, message)

    system = f"You are a helpful assistant. User preferences: {json.dumps(prefs)}"
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": message}],
    )
    return response.content[0].text


print(smart_onboarding_chat("new_user_alice", "I work with Python for data analysis. Keep it brief."))
```

**Expected Token Savings**: Cold-start users get personalized responses immediately without multiple clarification turns.
**Environment**: File-based profiles. Replace hash embeddings with real embeddings (voyage-3-lite) for production.

---

### Option 4: Redis-Backed Preference Store for Multi-Service Agents

Centralized preference store shared across multiple agent services and instances.

```python
import json
import redis
import anthropic
from datetime import datetime, timezone

r = redis.Redis(host="localhost", port=6379, decode_responses=True)
client = anthropic.Anthropic()

PREF_TTL = 60 * 60 * 24 * 90  # 90 days


def pref_key(user_id: str) -> str:
    return f"agent:prefs:{user_id}"


def load_preferences(user_id: str) -> dict:
    raw = r.get(pref_key(user_id))
    return json.loads(raw) if raw else {}


def save_preference(user_id: str, key: str, value: str):
    key_name = pref_key(user_id)
    prefs = load_preferences(user_id)
    prefs[key] = {"value": value, "updated": datetime.now(timezone.utc).isoformat()}
    r.set(key_name, json.dumps(prefs), ex=PREF_TTL)
    print(f"  [Redis] Saved {key}={value} for {user_id}")


def get_pref_value(user_id: str, key: str, default: str = "") -> str:
    prefs = load_preferences(user_id)
    entry = prefs.get(key, {})
    return entry.get("value", default) if isinstance(entry, dict) else default


EXTRACTION_TOOL = {
    "name": "save_user_preference",
    "description": "Save a user preference to persistent storage.",
    "input_schema": {
        "type": "object",
        "properties": {
            "preference_key": {"type": "string", "description": "Preference identifier"},
            "preference_value": {"type": "string", "description": "Value to store"},
        },
        "required": ["preference_key", "preference_value"],
    },
}


def build_system(user_id: str) -> str:
    prefs = load_preferences(user_id)
    if not prefs:
        return "You are a helpful assistant. Use the save_user_preference tool when users state preferences."

    pref_lines = "\n".join(
        f"  {k}: {v['value'] if isinstance(v, dict) else v}"
        for k, v in prefs.items()
    )
    return (
        "You are a helpful assistant.\n\n"
        f"Known preferences for this user:\n{pref_lines}\n\n"
        "Use save_user_preference when you detect new preferences."
    )


def chat(user_id: str, message: str) -> str:
    messages = [{"role": "user", "content": message}]
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=build_system(user_id),
        tools=[EXTRACTION_TOOL],
        messages=messages,
    )

    tool_results = []
    reply_text = ""

    for block in response.content:
        if block.type == "tool_use":
            save_preference(user_id, block.input["preference_key"], block.input["preference_value"])
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": "Saved.",
            })
        elif block.type == "text":
            reply_text = block.text

    if tool_results:
        final = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=build_system(user_id),
            tools=[EXTRACTION_TOOL],
            messages=[
                {"role": "user", "content": message},
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": tool_results},
            ],
        )
        reply_text = final.content[0].text

    return reply_text


print(chat("alice", "I always want code in TypeScript and keep explanations under 3 sentences."))
print(chat("alice", "How do I make an HTTP request?"))  # Uses TypeScript preference
```

**Expected Token Savings**: Redis TTL ensures stale preferences don't persist indefinitely; shared store eliminates per-service preference duplication.
**Environment**: Redis required. Works across multiple processes and services.

---

### Option 5: Preference Versioning with Conflict Resolution

Track preference history and resolve conflicts when users state contradictory preferences.

```python
import json
import sqlite3
import anthropic
from datetime import datetime, timezone
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class PreferenceEntry:
    key: str
    value: str
    confidence: float  # 0.0–1.0: how certain we are this preference is stable
    source: str        # explicit | inferred
    updated: str


def init_versioned_db() -> sqlite3.Connection:
    conn = sqlite3.connect("versioned_prefs.db")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pref_history (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   TEXT,
            key       TEXT,
            value     TEXT,
            confidence REAL,
            source    TEXT,
            updated   TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pref_current (
            user_id   TEXT,
            key       TEXT,
            value     TEXT,
            confidence REAL,
            source    TEXT,
            updated   TEXT,
            PRIMARY KEY (user_id, key)
        )
    """)
    conn.commit()
    return conn


conn = init_versioned_db()


def upsert_preference(user_id: str, key: str, value: str, confidence: float, source: str):
    now = datetime.now(timezone.utc).isoformat()
    # Check for conflict
    existing = conn.execute(
        "SELECT value, confidence FROM pref_current WHERE user_id=? AND key=?",
        (user_id, key)
    ).fetchone()

    if existing and existing[0] != value:
        old_val, old_conf = existing
        print(f"  [Conflict] {key}: '{old_val}' (conf={old_conf:.1f}) → '{value}' (conf={confidence:.1f})")
        # Higher confidence wins; if equal, newer wins
        if confidence < old_conf:
            print(f"  [Keeping existing: lower confidence for new value]")
            return

    conn.execute(
        "INSERT INTO pref_history (user_id, key, value, confidence, source, updated) VALUES (?,?,?,?,?,?)",
        (user_id, key, value, confidence, source, now)
    )
    conn.execute(
        """INSERT INTO pref_current (user_id, key, value, confidence, source, updated) VALUES (?,?,?,?,?,?)
           ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, confidence=excluded.confidence,
           source=excluded.source, updated=excluded.updated""",
        (user_id, key, value, confidence, source, now)
    )
    conn.commit()
    print(f"  [Saved] {key}={value} (confidence={confidence:.1f}, source={source})")


def get_preferences(user_id: str) -> list[PreferenceEntry]:
    rows = conn.execute(
        "SELECT key, value, confidence, source, updated FROM pref_current WHERE user_id=? ORDER BY confidence DESC",
        (user_id,)
    ).fetchall()
    return [PreferenceEntry(*row) for row in rows]


PREF_EXTRACT_TOOL = {
    "name": "record_preference",
    "description": "Record a detected user preference with confidence level.",
    "input_schema": {
        "type": "object",
        "properties": {
            "key": {"type": "string"},
            "value": {"type": "string"},
            "confidence": {
                "type": "number",
                "description": "0.0=inferred, 0.5=likely, 1.0=explicitly stated",
            },
            "source": {"type": "string", "enum": ["explicit", "inferred"]},
        },
        "required": ["key", "value", "confidence", "source"],
    },
}


def versioned_chat(user_id: str, message: str) -> str:
    prefs = get_preferences(user_id)
    pref_context = "\n".join(
        f"  {p.key}: {p.value} (confidence: {p.confidence:.0%})"
        for p in prefs
    ) or "  (none yet)"

    system = f"""You are a helpful assistant with memory of user preferences.

Current preferences:
{pref_context}

Use record_preference when you detect preferences. Set confidence=1.0 for explicit statements,
confidence=0.5 for clear implications, confidence=0.2 for weak inferences."""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=system,
        tools=[PREF_EXTRACT_TOOL],
        messages=[{"role": "user", "content": message}],
    )

    tool_results = []
    for block in response.content:
        if block.type == "tool_use":
            inp = block.input
            upsert_preference(user_id, inp["key"], inp["value"], inp["confidence"], inp["source"])
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": "Saved."})

    if tool_results:
        final = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            tools=[PREF_EXTRACT_TOOL],
            messages=[
                {"role": "user", "content": message},
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": tool_results},
            ],
        )
        return final.content[0].text

    return next((b.text for b in response.content if b.type == "text"), "")


print(versioned_chat("alice", "I explicitly want Python always."))
print(versioned_chat("alice", "Actually, this project uses TypeScript."))  # Conflict resolution
```

**Expected Token Savings**: Confidence scores prevent low-quality inferences from overriding explicit preferences; reduces clarification ping-pong.
**Environment**: SQLite with history table. Full audit trail for debugging preference conflicts.

---

### Option 6: Preference-Aware System Prompt Builder

Aggregate all preferences into a concise, token-efficient system prompt injection.

```python
import json
import anthropic
from pathlib import Path

client = anthropic.Anthropic()
PREF_FILE = Path("prefs_store.json")


def load_all() -> dict:
    return json.loads(PREF_FILE.read_text()) if PREF_FILE.exists() else {}


def save_user_pref(user_id: str, key: str, value: str):
    store = load_all()
    store.setdefault(user_id, {})[key] = value
    PREF_FILE.write_text(json.dumps(store, indent=2))


def build_compact_system(user_id: str, base_system: str) -> str:
    """
    Build a token-efficient system prompt that injects preferences
    as a compact key:value block (uses ~30 tokens per preference).
    """
    store = load_all()
    prefs = store.get(user_id, {})
    if not prefs:
        return base_system

    pref_block = "User profile: " + "; ".join(f"{k}={v}" for k, v in prefs.items())
    return f"{base_system}\n\n{pref_block}"


LEARN_TOOL = {
    "name": "learn_preference",
    "description": "Persist a user preference for future sessions.",
    "input_schema": {
        "type": "object",
        "properties": {
            "key":   {"type": "string", "description": "Short snake_case key"},
            "value": {"type": "string", "description": "Preference value"},
        },
        "required": ["key", "value"],
    },
}

BASE_SYSTEM = """You are a helpful coding assistant.
Detect and save user preferences using the learn_preference tool.
Apply all known preferences to every response."""


def personalized_chat(user_id: str, messages: list[dict]) -> str:
    system = build_compact_system(user_id, BASE_SYSTEM)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=system,
        tools=[LEARN_TOOL],
        messages=messages,
    )

    tool_results = []
    reply_text = ""

    for block in response.content:
        if block.type == "tool_use" and block.name == "learn_preference":
            save_user_pref(user_id, block.input["key"], block.input["value"])
            print(f"  [Learned] {block.input['key']} = {block.input['value']}")
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": "Saved."})
        elif block.type == "text":
            reply_text = block.text

    if tool_results:
        messages_with_result = messages + [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": tool_results},
        ]
        final = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=build_compact_system(user_id, BASE_SYSTEM),
            tools=[LEARN_TOOL],
            messages=messages_with_result,
        )
        reply_text = final.content[0].text

    return reply_text


# Session 1
msgs = [{"role": "user", "content": "I write Go. Keep examples under 10 lines. I hate verbose explanations."}]
print(personalized_chat("bob", msgs))

# Session 2 — new session, preferences loaded from file
msgs2 = [{"role": "user", "content": "How do I read a file line by line?"}]
print(personalized_chat("bob", msgs2))  # automatically uses Go, concise style
```

**Expected Token Savings**: Compact `key=value` format uses ~5 tokens per preference vs 20+ for natural language. 10 preferences = 50 tokens instead of 200+.
**Environment**: JSON file storage. Zero external dependencies.

---

| Option | Storage | Conflict Handling | Multi-Service | Best For |
|--------|---------|------------------|--------------|----------|
| 1 | SQLite auto-extract | Overwrite | No | Simple persistent memory |
| 2 | JSON + explicit schema | Schema-validated | No | Inspectable, user-editable prefs |
| 3 | File + embeddings | Similarity bootstrap | No | Cold-start personalization |
| 4 | Redis | Confidence-based | Yes | Multi-service, distributed agents |
| 5 | SQLite + history | Confidence + audit trail | No | High-stakes preference management |
| 6 | JSON compact injection | Last-write-wins | No | Token-efficient injection |
