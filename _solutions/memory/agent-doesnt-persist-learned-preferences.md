---
layout: solution
title: "Agent Doesn't Persist Learned Preferences"
category: memory
description: "User teaches the agent their preferences ('always use metric units', 'I prefer concise answers') but the agent forgets them at the start of every new session."
tags: [memory, persistence, preferences, user-experience, personalisation]
---

## Symptom

A user spends the first five minutes of every session re-teaching the agent: "Remember, I'm a Python developer, I don't need JavaScript examples." "Use metric units." "I want bullet points, not paragraphs." The agent learns perfectly within a session, then loses everything when the session ends. The next session starts from zero. Power users are perpetually frustrated; casual users give up.

## Root Cause

LLM context is ephemeral. Nothing that happens within a conversation persists to the next unless the application explicitly stores it. Most agent implementations treat each session as a fresh start with only the static system prompt. Even when users explicitly say "remember this", the model has no write path to durable storage.

## Fix

### Option 1 — File-based preference store loaded at session start

```python
import json
import os
import anthropic

client    = anthropic.Anthropic()
PREFS_DIR = os.path.expanduser("~/.agent_prefs")
os.makedirs(PREFS_DIR, exist_ok=True)

def prefs_path(user_id: str) -> str:
    return os.path.join(PREFS_DIR, f"{user_id}.json")

def load_prefs(user_id: str) -> dict:
    try:
        with open(prefs_path(user_id)) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_prefs(user_id: str, prefs: dict) -> None:
    with open(prefs_path(user_id), "w") as f:
        json.dump(prefs, f, indent=2)
    print(f"[prefs] saved: {prefs}")

def build_system(prefs: dict) -> str:
    base = "You are a helpful assistant."
    if not prefs:
        return base
    pref_lines = "\n".join(f"- {k}: {v}" for k, v in prefs.items())
    return f"{base}\n\nUser preferences (always follow these):\n{pref_lines}"

EXTRACT_SYSTEM = """Extract any user preference from this message and return JSON.
Keys: unit_system, response_format, language, expertise_level, topics_to_avoid, or other relevant keys.
Return {{}} if no preference is stated. Only return JSON, no other text."""

def extract_preference(message: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=EXTRACT_SYSTEM,
        messages=[{"role": "user", "content": message}],
    )
    raw = response.content[0].text.strip().lstrip("```json").rstrip("```").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}

def chat(user_id: str, history: list[dict], user_message: str) -> tuple[str, list[dict], dict]:
    prefs = load_prefs(user_id)

    # Detect and store preferences from this message
    new_prefs = extract_preference(user_message)
    if new_prefs:
        prefs.update(new_prefs)
        save_prefs(user_id, prefs)

    history = history + [{"role": "user", "content": user_message}]
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=build_system(prefs),
        messages=history,
    )
    reply = response.content[0].text
    history = history + [{"role": "assistant", "content": reply}]
    return reply, history, prefs

# Session 1 — teach preferences
print("=== Session 1 ===")
history = []
for msg in [
    "Always use metric units when giving measurements.",
    "How far is New York from London?",
    "I prefer concise answers, bullet points over paragraphs.",
    "What are the top 3 Python web frameworks?",
]:
    reply, history, prefs = chat("user-42", history, msg)
    print(f"User: {msg}")
    print(f"Agent: {reply[:150]}\n")

# Session 2 — preferences are loaded automatically
print("=== Session 2 (new session, prefs restored) ===")
history = []
for msg in [
    "What is the distance from Paris to Berlin?",
    "What are some good databases for Python apps?",
]:
    reply, history, prefs = chat("user-42", history, msg)
    print(f"User: {msg}")
    print(f"Agent: {reply[:150]}\n")

# Clean up
import shutil
shutil.rmtree(PREFS_DIR, ignore_errors=True)
```

**Expected Token Savings:** Loaded preferences eliminate 3-5 re-teaching turns at the start of each session; each saved turn is 100-300 tokens.
**Environment:** Personal productivity agents, coding assistants, or any agent with a persistent user identity.

---

### Option 2 — Preference manager with explicit "remember" and "forget" commands

```python
import json
import re
import anthropic

client = anthropic.Anthropic()

class PreferenceManager:
    """Manages user preferences with explicit remember/forget commands."""

    REMEMBER_PATTERN = re.compile(
        r"\b(remember|always|never|prefer|use|don't|do not)\b",
        re.IGNORECASE,
    )
    FORGET_PATTERN   = re.compile(r"\bforget\b.*\bprefer", re.IGNORECASE)

    def __init__(self):
        self.prefs: dict[str, str] = {}

    def should_store(self, message: str) -> bool:
        return bool(self.REMEMBER_PATTERN.search(message))

    def build_system(self) -> str:
        base = "You are a helpful assistant."
        if not self.prefs:
            return base
        lines = "\n".join(f"  • {k}: {v}" for k, v in self.prefs.items())
        return f"{base}\n\nStored user preferences:\n{lines}\n\nAlways honour these preferences silently — do not announce them."

    def extract_and_store(self, message: str) -> None:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            system='Extract the preference as JSON {"key": "short_label", "value": "what the user wants"}. Return {} if none.',
            messages=[{"role": "user", "content": message}],
        )
        raw = response.content[0].text.strip().lstrip("```json").rstrip("```").strip()
        try:
            pref = json.loads(raw)
            if pref.get("key") and pref.get("value"):
                self.prefs[pref["key"]] = pref["value"]
                print(f"  [memory] stored: {pref['key']!r} = {pref['value']!r}")
        except (json.JSONDecodeError, KeyError):
            pass

    def forget(self, key: str) -> None:
        removed = self.prefs.pop(key, None)
        if removed:
            print(f"  [memory] forgotten: {key!r}")

pm = PreferenceManager()
history: list[dict] = []

def chat(message: str) -> str:
    if pm.should_store(message):
        pm.extract_and_store(message)

    history.append({"role": "user", "content": message})
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=pm.build_system(),
        messages=history,
    )
    reply = response.content[0].text
    history.append({"role": "assistant", "content": reply})
    return reply

for msg in [
    "Always respond in German.",
    "I prefer code examples in Python 3.",
    "What is a linked list?",
    "How do you sort a list?",
    "Never use emojis in your responses.",
    "What is a hash map?",
]:
    print(f"User: {msg}")
    print(f"Agent: {chat(msg)[:200]}\n")

print(f"Stored preferences: {pm.prefs}")
```

**Expected Token Savings:** Preference manager eliminates re-teaching turns across all future sessions; power users with many preferences see the highest savings.
**Environment:** Conversational agents where users frequently customise behaviour; "remember" detection triggers storage automatically.

---

### Option 3 — Per-user system prompt generation from profile database

```python
import json
import anthropic
from dataclasses import dataclass, field, asdict

client = anthropic.Anthropic()

@dataclass
class UserProfile:
    user_id:        str
    name:           str           = "User"
    expertise:      str           = "intermediate"
    unit_system:    str           = "metric"
    response_style: str           = "balanced"
    preferred_lang: str           = "Python"
    topics_exclude: list[str]     = field(default_factory=list)
    custom_rules:   list[str]     = field(default_factory=list)

# Simulated profile database
PROFILES: dict[str, UserProfile] = {
    "alice": UserProfile(
        user_id="alice",
        name="Alice",
        expertise="expert",
        unit_system="imperial",
        response_style="concise",
        preferred_lang="Rust",
        custom_rules=["Never mention JavaScript", "Always include time complexity for algorithms"],
    ),
    "bob": UserProfile(
        user_id="bob",
        name="Bob",
        expertise="beginner",
        unit_system="metric",
        response_style="detailed",
        preferred_lang="Python",
        topics_exclude=["cryptocurrency"],
        custom_rules=["Explain jargon when you use it", "Use real-world analogies"],
    ),
}

def build_personalised_system(profile: UserProfile) -> str:
    rules = "\n".join(f"- {r}" for r in profile.custom_rules) if profile.custom_rules else "None."
    exclude = ", ".join(profile.topics_exclude) if profile.topics_exclude else "None."
    return f"""You are a helpful assistant talking to {profile.name}.

User profile:
- Expertise level: {profile.expertise}
- Unit system: {profile.unit_system}
- Response style: {profile.response_style} ({"bullet points" if profile.response_style == "concise" else "thorough explanations"})
- Preferred programming language: {profile.preferred_lang}
- Topics to avoid: {exclude}

Custom rules:
{rules}

Tailor every response to this profile without announcing that you're doing so."""

def ask_for_user(user_id: str, question: str) -> str:
    profile = PROFILES.get(user_id)
    if not profile:
        system = "You are a helpful assistant."
    else:
        system = build_personalised_system(profile)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text

question = "How do I sort a list of items efficiently?"
for uid in ["alice", "bob", "unknown"]:
    print(f"[{uid}] {ask_for_user(uid, question)[:250]}\n")
```

**Expected Token Savings:** Profile-based system prompts front-load all preferences in one call instead of spreading them across multiple re-teaching turns.
**Environment:** Multi-user platforms where user profiles are already stored; system prompt generation from profile is a zero-added-latency personalisation layer.

---

### Option 4 — Preference learning from implicit feedback

```python
import json
import anthropic

client = anthropic.Anthropic()

class ImplicitFeedbackLearner:
    """
    Detects satisfaction signals in user messages and updates preferences.
    Positive: "great", "exactly", "perfect", "yes"
    Negative: "too long", "not that", "wrong format", "shorter please"
    """
    POSITIVE = {"great", "perfect", "exactly", "thanks", "yes", "correct", "good", "love it"}
    NEGATIVE_PATTERNS = [
        ("too long",     {"response_style": "concise"}),
        ("too short",    {"response_style": "detailed"}),
        ("simpler",      {"expertise": "beginner"}),
        ("more detail",  {"response_style": "detailed"}),
        ("bullet",       {"format": "bullet points"}),
        ("paragraph",    {"format": "prose"}),
        ("metric",       {"units": "metric"}),
        ("imperial",     {"units": "imperial"}),
    ]

    def __init__(self):
        self.prefs: dict[str, str] = {}
        self.history: list[dict] = []
        self.last_response: str = ""

    def detect_and_learn(self, user_message: str) -> None:
        msg_lower = user_message.lower()

        # Detect negative feedback → update preference
        for trigger, new_pref in self.NEGATIVE_PATTERNS:
            if trigger in msg_lower:
                self.prefs.update(new_pref)
                print(f"  [learn] implicit feedback '{trigger}' → prefs updated: {new_pref}")

        # Detect positive feedback → reinforce last response style
        if any(word in msg_lower.split() for word in self.POSITIVE):
            print(f"  [learn] positive signal — reinforcing current preferences")

    def build_system(self) -> str:
        base = "You are a helpful, adaptive assistant."
        if not self.prefs:
            return base
        pref_str = ", ".join(f"{k}={v}" for k, v in self.prefs.items())
        return f"{base} User preferences: {pref_str}. Apply silently."

    def chat(self, user_message: str) -> str:
        self.detect_and_learn(user_message)
        self.history.append({"role": "user", "content": user_message})
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=self.build_system(),
            messages=self.history,
        )
        reply = response.content[0].text
        self.last_response = reply
        self.history.append({"role": "assistant", "content": reply})
        return reply

learner = ImplicitFeedbackLearner()

for msg in [
    "Explain how a binary search tree works.",
    "That was too long. Give a shorter answer.",
    "What is a hash table?",
    "Perfect! Now explain a queue.",
    "Can you use bullet points?",
    "What is a stack?",
]:
    print(f"User: {msg}")
    print(f"Agent: {learner.chat(msg)[:200]}\n")

print(f"Learned preferences: {learner.prefs}")
```

**Expected Token Savings:** Implicit feedback learning adapts to user style without explicit "remember" commands; reduces correction turns as the model converges on preferred format.
**Environment:** Long-session assistants where explicit preference teaching feels unnatural; implicit feedback is lower friction.

---

### Option 5 — Preferences stored in conversation memory summary

```python
import json
import anthropic

client = anthropic.Anthropic()

PREFERENCE_EXTRACTOR_SYSTEM = """Analyse this conversation and extract all user preferences, constraints, and requirements.
Output JSON:
{
  "preferences": [{"key": str, "value": str, "confidence": "high"|"medium"|"low"}],
  "topics_discussed": [str],
  "user_expertise_signal": "beginner"|"intermediate"|"expert"|"unknown"
}"""

def extract_session_prefs(history: list[dict]) -> dict:
    """Called at end of session to extract and persist preferences."""
    if len(history) < 4:
        return {}

    convo = "\n".join(f"{m['role'].upper()}: {m['content'][:150]}" for m in history)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=PREFERENCE_EXTRACTOR_SYSTEM,
        messages=[{"role": "user", "content": convo}],
    )
    raw = response.content[0].text.strip().lstrip("```json").rstrip("```").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}

def load_memory(user_id: str) -> dict:
    try:
        with open(f"/tmp/agent_memory_{user_id}.json") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_memory(user_id: str, memory: dict) -> None:
    with open(f"/tmp/agent_memory_{user_id}.json", "w") as f:
        json.dump(memory, f, indent=2)

def build_system_from_memory(memory: dict) -> str:
    if not memory:
        return "You are a helpful assistant."
    prefs = memory.get("preferences", [])
    high_conf = [p for p in prefs if p.get("confidence") == "high"]
    if not high_conf:
        return "You are a helpful assistant."
    pref_lines = "\n".join(f"- {p['key']}: {p['value']}" for p in high_conf)
    expertise  = memory.get("user_expertise_signal", "unknown")
    return f"""You are a helpful assistant.
User expertise: {expertise}.
Known preferences from previous sessions:
{pref_lines}
Apply these silently without confirming them."""

def run_session(user_id: str, messages: list[str]) -> list[dict]:
    memory = load_memory(user_id)
    system = build_system_from_memory(memory)
    print(f"  [session] loaded memory: {len(memory.get('preferences', []))} preferences")

    history: list[dict] = []
    for msg in messages:
        history.append({"role": "user", "content": msg})
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=system,
            messages=history,
        )
        reply = response.content[0].text
        history.append({"role": "assistant", "content": reply})

    # End of session: extract and persist preferences
    extracted = extract_session_prefs(history)
    if extracted:
        # Merge with existing memory
        existing = memory.get("preferences", [])
        new_keys = {p["key"] for p in extracted.get("preferences", [])}
        merged = [p for p in existing if p["key"] not in new_keys]
        merged.extend(extracted.get("preferences", []))
        memory["preferences"] = merged
        memory["user_expertise_signal"] = extracted.get("user_expertise_signal", "unknown")
        save_memory(user_id, memory)
        print(f"  [session] saved {len(merged)} preferences")

    return history

# Session 1 — user reveals preferences naturally
print("=== Session 1 ===")
history1 = run_session("user-77", [
    "I'm a senior DevOps engineer, been using Linux for 15 years.",
    "Always give me bash commands, not Python scripts.",
    "How do I check disk usage by directory?",
    "Prefer one-liners when possible.",
    "What's the fastest way to find large files?",
])
for m in history1[-4:]:
    print(f"  {m['role'].upper()}: {m['content'][:100]}")

# Session 2 — preferences are loaded
print("\n=== Session 2 ===")
history2 = run_session("user-77", [
    "How do I monitor CPU usage in real time?",
])
for m in history2:
    print(f"  {m['role'].upper()}: {m['content'][:150]}")

import os
os.remove("/tmp/agent_memory_user-77.json")
```

**Expected Token Savings:** End-of-session extraction runs once per session; preferences loaded at session start eliminate N re-teaching turns where N grows with user tenure.
**Environment:** Long-term user relationships; preference extraction from natural conversation is lower-friction than explicit "remember" commands.

---

### Option 6 — Preference sync across devices via shared key-value store

```python
import json
import time
import hashlib
import anthropic

client = anthropic.Anthropic()

class SharedPreferenceStore:
    """
    Simulates a shared KV store (Redis, DynamoDB, etc.) for cross-device preference sync.
    In production, replace _store with actual database calls.
    """
    _store: dict[str, str] = {}   # shared across all instances (simulates DB)

    def __init__(self, user_id: str):
        self.user_id = user_id
        self._namespace = f"prefs:{user_id}"

    def get_all(self) -> dict:
        raw = SharedPreferenceStore._store.get(self._namespace, "{}")
        return json.loads(raw)

    def set(self, key: str, value: str) -> None:
        prefs = self.get_all()
        prefs[key] = value
        prefs["_updated_at"] = str(time.time())
        SharedPreferenceStore._store[self._namespace] = json.dumps(prefs)
        print(f"  [store] {self.user_id}: set {key!r} = {value!r}")

    def delete(self, key: str) -> None:
        prefs = self.get_all()
        prefs.pop(key, None)
        SharedPreferenceStore._store[self._namespace] = json.dumps(prefs)

def build_system(prefs: dict) -> str:
    if not prefs:
        return "You are a helpful assistant."
    lines = "\n".join(f"- {k}: {v}" for k, v in prefs.items() if not k.startswith("_"))
    return f"You are a helpful assistant.\n\nUser preferences:\n{lines}\n\nFollow all preferences silently."

def chat_device(user_id: str, device_name: str, history: list[dict], message: str) -> tuple[str, list[dict]]:
    store = SharedPreferenceStore(user_id)
    prefs = store.get_all()

    # Detect preference updates
    if any(kw in message.lower() for kw in ["prefer", "always", "never", "use", "don't"]):
        detect_resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            system='Extract preference as {"key": str, "value": str} or {}.',
            messages=[{"role": "user", "content": message}],
        )
        raw = detect_resp.content[0].text.strip().lstrip("```json").rstrip("```").strip()
        try:
            pref = json.loads(raw)
            if pref.get("key"):
                store.set(pref["key"], pref["value"])
                prefs = store.get_all()
        except json.JSONDecodeError:
            pass

    history = history + [{"role": "user", "content": message}]
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=build_system(prefs),
        messages=history,
    )
    reply = response.content[0].text
    history = history + [{"role": "assistant", "content": reply}]
    print(f"  [{device_name}] {reply[:120]}")
    return reply, history

# Teach preference on device A
hist_a: list[dict] = []
_, hist_a = chat_device("user-99", "MacBook", hist_a, "Always use TypeScript, not JavaScript.")
_, hist_a = chat_device("user-99", "MacBook", hist_a, "How do I define an interface?")

# New session on device B — preference is already stored
print("\n--- New session on iPhone ---")
hist_b: list[dict] = []
_, hist_b = chat_device("user-99", "iPhone",  hist_b, "How do I make an HTTP request?")
```

**Expected Token Savings:** Cross-device sync means preferences taught on one device are available on all others; eliminates re-teaching for multi-device users.
**Environment:** Consumer agents accessed across web, mobile, and desktop; shared store is the production pattern for any multi-device deployment.

---

## Comparison

| Option | Storage Backend | Cross-Session | Cross-Device | Automatic Learning | Best For |
|---|---|---|---|---|---|
| 1. File-based store | Local JSON file | Yes | No | Yes (extraction) | Single-device personal agents |
| 2. Explicit remember/forget | In-memory + file | Yes | No | Partial | Power users who prefer explicit control |
| 3. Profile database | DB (simulated) | Yes | Yes | No | Multi-user platforms with existing user DB |
| 4. Implicit feedback | In-memory | No | No | Yes | Session-level style adaptation |
| 5. Session summary extraction | Local JSON | Yes | No | Yes | Natural-language preference teaching |
| 6. Shared KV store | Remote KV (simulated) | Yes | Yes | Yes | Multi-device consumer apps |
