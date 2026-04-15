---
layout: solution
title: "Agent Loses Track of User Preferences Across Sessions"
category: memory
description: "Agent starts each session with no memory of what it learned about the user — repeating onboarding questions, ignoring established preferences, and providing inconsistent behaviour."
tags: [memory, persistence, user-preferences, sessions, personalisation]
---

## Symptom

Agent treats every session as a fresh start:

```
Session 1:
  User: "Please always respond in bullet points, I prefer concise answers"
  Agent: "Understood! I'll use bullet points."

Session 2 (next day):
  User: "What's the difference between async and threading?"
  Agent: [writes 4 paragraphs of prose]
  User: "I told you yesterday I prefer bullet points!"
  Agent: "I apologise, I don't have memory of previous conversations."

Session 3:
  User: "What's your recommended Python web framework?"
  Agent: "What's your experience level and use case?"
  User: "I literally answered this last week — I'm a senior engineer building APIs"
  Agent: "I apologise, each session starts fresh..."
```

Users must re-state their preferences every session. Personalization is impossible. High-value users who invested time training the agent are frustrated when that context evaporates.

## Root Cause

LLMs are stateless. Each API call begins with a blank slate; there is no automatic persistence of conversation history between sessions. Without an explicit persistence layer that stores learned preferences and reloads them into subsequent session contexts, the agent can never accumulate a model of its users.

## Fix

---

### Option 1: JSON Preference Store — Persist and Inject on Session Start

Maintain a simple JSON file per user with learned preferences. Load it into the system prompt at the start of every session.

```python
import json
from pathlib import Path
import anthropic

PREF_DIR = Path(".user_prefs")
PREF_DIR.mkdir(exist_ok=True)

def load_prefs(user_id: str) -> dict:
    path = PREF_DIR / f"{user_id}.json"
    if path.exists():
        return json.loads(path.read_text())
    return {
        "response_format": None,
        "expertise_level": None,
        "preferred_language": None,
        "topics_of_interest": [],
        "disliked_patterns": [],
        "custom_notes": [],
    }

def save_prefs(user_id: str, prefs: dict) -> None:
    path = PREF_DIR / f"{user_id}.json"
    path.write_text(json.dumps(prefs, indent=2))

def build_system_prompt(prefs: dict) -> str:
    base = "You are a helpful assistant."
    if not any(prefs.values()):
        return base

    lines = [base, "\n## User Preferences (learned from prior sessions):"]
    if prefs.get("response_format"):
        lines.append(f"- Format: {prefs['response_format']}")
    if prefs.get("expertise_level"):
        lines.append(f"- Expertise: {prefs['expertise_level']}")
    if prefs.get("preferred_language"):
        lines.append(f"- Language: {prefs['preferred_language']}")
    if prefs.get("topics_of_interest"):
        lines.append(f"- Interests: {', '.join(prefs['topics_of_interest'])}")
    if prefs.get("disliked_patterns"):
        lines.append(f"- Avoid: {', '.join(prefs['disliked_patterns'])}")
    if prefs.get("custom_notes"):
        for note in prefs["custom_notes"]:
            lines.append(f"- Note: {note}")
    lines.append("\nAlways follow these preferences without needing reminders.")
    return "\n".join(lines)

def extract_new_prefs(conversation: list[dict], existing_prefs: dict) -> dict:
    """Use model to extract any new preference signals from the session."""
    client = anthropic.Anthropic()
    convo_text = "\n".join(f"{m['role']}: {str(m['content'])[:200]}" for m in conversation[-10:])
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system="""Extract user preferences from this conversation. Return a JSON object with only
CHANGED or NEW preferences (omit unchanged fields). Use null to clear a preference.
Fields: response_format, expertise_level, preferred_language, topics_of_interest (list),
disliked_patterns (list), custom_notes (list).""",
        messages=[{
            "role": "user",
            "content": f"Existing prefs: {json.dumps(existing_prefs)}\n\nConversation:\n{convo_text}",
        }],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
    try:
        updates = json.loads(raw)
        merged = {**existing_prefs}
        for k, v in updates.items():
            if v is None:
                merged[k] = None if not isinstance(existing_prefs.get(k), list) else []
            elif isinstance(v, list) and isinstance(merged.get(k), list):
                # Merge lists, deduplicate
                merged[k] = list(dict.fromkeys(merged[k] + v))
            else:
                merged[k] = v
        return merged
    except Exception:
        return existing_prefs

def chat_session(user_id: str) -> None:
    prefs = load_prefs(user_id)
    system = build_system_prompt(prefs)
    client = anthropic.Anthropic()
    messages: list[dict] = []

    print(f"Session started for {user_id}. Known prefs: {[k for k, v in prefs.items() if v]}")

    # Simulate a conversation
    user_inputs = [
        "Explain Python's GIL in bullet points please",
        "I'm a senior engineer, you don't need to explain basics",
    ]

    for user_input in user_inputs:
        messages.append({"role": "user", "content": user_input})
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=system,
            messages=messages,
        )
        reply = response.content[0].text
        messages.append({"role": "assistant", "content": reply})
        print(f"User: {user_input}\nAgent: {reply[:100]}...\n")

    # End of session: extract and save updated preferences
    updated_prefs = extract_new_prefs(messages, prefs)
    if updated_prefs != prefs:
        save_prefs(user_id, updated_prefs)
        print(f"Preferences updated: {updated_prefs}")

chat_session("user_42")
```

**Expected Token Savings:** Preference injection: ~150 tokens per session. Eliminates re-explanation overhead (typically 2-3 turns × 300 tokens = 600-900 tokens). Net savings: 450-750 tokens/session after break-even.
**Environment:** File-based storage works for single-instance deployments. For multi-instance, use a shared database (PostgreSQL, DynamoDB) keyed by user_id.

---

### Option 2: Structured Preference Schema with Versioning

Define a typed schema for preferences with version tracking so preference evolution is explicit and auditable.

```python
import json
from pathlib import Path
from datetime import datetime
from pydantic import BaseModel, Field
import anthropic

class UserPreferences(BaseModel):
    version: int = 1
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    # Communication
    response_format: str = "prose"               # prose | bullets | numbered | concise
    response_length: str = "medium"              # short | medium | long | adaptive
    use_code_examples: bool = True
    preferred_language: str = "English"
    # Technical level
    expertise_domains: dict[str, str] = Field(default_factory=dict)  # domain → level
    assume_familiarity_with: list[str] = Field(default_factory=list)
    # Interaction style
    wants_proactive_suggestions: bool = True
    wants_caveats_and_warnings: bool = True
    dislikes: list[str] = Field(default_factory=list)
    # Project context
    current_project: str = ""
    tech_stack: list[str] = Field(default_factory=list)

PREF_DIR = Path(".user_prefs_v2")
PREF_DIR.mkdir(exist_ok=True)

def load_preferences(user_id: str) -> UserPreferences:
    path = PREF_DIR / f"{user_id}.json"
    if path.exists():
        return UserPreferences(**json.loads(path.read_text()))
    return UserPreferences()

def save_preferences(user_id: str, prefs: UserPreferences) -> None:
    prefs.updated_at = datetime.now().isoformat()
    prefs.version += 1
    (PREF_DIR / f"{user_id}.json").write_text(prefs.model_dump_json(indent=2))

def preferences_to_system_context(prefs: UserPreferences) -> str:
    parts = []
    if prefs.expertise_domains:
        domain_str = "; ".join(f"{d}={l}" for d, l in prefs.expertise_domains.items())
        parts.append(f"User expertise: {domain_str}")
    if prefs.assume_familiarity_with:
        parts.append(f"Skip explaining: {', '.join(prefs.assume_familiarity_with)}")
    parts.append(f"Response format: {prefs.response_format}, length: {prefs.response_length}")
    if prefs.tech_stack:
        parts.append(f"Tech stack: {', '.join(prefs.tech_stack)}")
    if prefs.current_project:
        parts.append(f"Current project: {prefs.current_project}")
    if prefs.dislikes:
        parts.append(f"Avoid: {', '.join(prefs.dislikes)}")
    if not prefs.wants_caveats_and_warnings:
        parts.append("Skip obvious caveats and warnings")
    return "\n".join(parts)

def update_preferences_from_session(
    user_id: str, session_notes: list[str]
) -> UserPreferences:
    prefs = load_preferences(user_id)
    client = anthropic.Anthropic()

    notes_text = "\n".join(f"- {n}" for n in session_notes)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=f"""Update user preferences based on session observations.
Current preferences: {prefs.model_dump_json()}
Return a JSON object with only fields that should change.""",
        messages=[{"role": "user", "content": f"Session observations:\n{notes_text}"}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
    try:
        updates = json.loads(raw)
        updated = UserPreferences(**{**prefs.model_dump(), **updates})
        save_preferences(user_id, updated)
        return updated
    except Exception:
        return prefs

def run_session(user_id: str, query: str) -> str:
    prefs = load_preferences(user_id)
    pref_context = preferences_to_system_context(prefs)
    system = f"You are a helpful assistant.\n\n{pref_context}" if pref_context else "You are a helpful assistant."

    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": query}],
    )
    return response.content[0].text

# After a session where user said "I work with FastAPI and SQLAlchemy, keep answers concise"
update_preferences_from_session("user_42", [
    "User asked for concise answers",
    "User mentioned FastAPI and SQLAlchemy stack",
    "User is building REST APIs",
    "User has senior Python expertise",
])

result = run_session("user_42", "How do I handle database transactions in FastAPI?")
print(result)
```

**Expected Token Savings:** Typed schema prevents preference bloat — only meaningful fields are stored. Version tracking enables A/B testing of preference injection formats. Concise context injection (~100 tokens) vs verbose re-explanation in each session (~600 tokens) = 500 tokens saved per session.
**Environment:** Pydantic schema validates preferences and prevents corruption. Version field enables migration when schema evolves. Store in PostgreSQL using `JSONB` for multi-user production.

---

### Option 3: Preference Learning Agent — Dedicate a Sidecar to Tracking

Run a lightweight sidecar coroutine that monitors the main conversation and asynchronously updates preferences without blocking responses.

```python
import asyncio
import json
from pathlib import Path
from datetime import datetime
import anthropic

PREF_FILE = Path(".preferences_sidecar.json")

class PreferenceSidecar:
    """Runs alongside the main agent, learning preferences asynchronously."""

    def __init__(self, user_id: str):
        self.user_id = user_id
        self._prefs = self._load()
        self._update_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._client = anthropic.AsyncAnthropic()

    def _load(self) -> dict:
        if PREF_FILE.exists():
            data = json.loads(PREF_FILE.read_text())
            return data.get(self.user_id, {})
        return {}

    def _save(self) -> None:
        existing = {}
        if PREF_FILE.exists():
            existing = json.loads(PREF_FILE.read_text())
        existing[self.user_id] = self._prefs
        PREF_FILE.write_text(json.dumps(existing, indent=2))

    def get_context(self) -> str:
        if not self._prefs:
            return ""
        lines = ["User preferences:"]
        for k, v in self._prefs.items():
            lines.append(f"  {k}: {v}")
        return "\n".join(lines)

    def observe(self, user_msg: str, agent_reply: str) -> None:
        """Queue a turn for async preference extraction (non-blocking)."""
        self._update_queue.put_nowait((user_msg, agent_reply))

    async def _process_updates(self) -> None:
        """Background coroutine — process queued turns."""
        while True:
            try:
                user_msg, agent_reply = await asyncio.wait_for(
                    self._update_queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue

            try:
                response = await self._client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=128,
                    system="Extract any user preference signals from this exchange. Return JSON or {} if none.",
                    messages=[{
                        "role": "user",
                        "content": f"User: {user_msg[:300]}\nAgent: {agent_reply[:300]}",
                    }],
                )
                raw = response.content[0].text.strip()
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
                updates = json.loads(raw)
                if updates:
                    self._prefs.update(updates)
                    self._save()
                    print(f"[Sidecar] Updated prefs: {updates}")
            except Exception as e:
                print(f"[Sidecar] Error: {e}")

    async def run(self) -> None:
        await self._process_updates()

async def main():
    sidecar = PreferenceSidecar("user_42")
    client = anthropic.AsyncAnthropic()

    # Start sidecar in background
    sidecar_task = asyncio.create_task(sidecar.run())

    pref_context = sidecar.get_context()
    system = f"You are a helpful assistant.\n{pref_context}".strip()

    messages = [
        {"role": "user", "content": "Use bullet points always. I hate long paragraphs."},
    ]

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        system=system,
        messages=messages,
    )
    reply = response.content[0].text

    # Sidecar observes the turn asynchronously
    sidecar.observe(messages[0]["content"], reply)

    # Give sidecar a moment to process
    await asyncio.sleep(2.0)
    sidecar_task.cancel()

    print(f"Reply: {reply[:100]}...")
    print(f"Learned prefs: {sidecar._prefs}")

asyncio.run(main())
```

**Expected Token Savings:** Sidecar runs using `claude-haiku-4-5-20251001` at ~50 tokens/turn for extraction. Main agent benefits from injected context without extra turns. ROI: 50 tokens spent on sidecar → 300+ tokens saved per session in re-explanation avoidance.
**Environment:** Async architecture only. Sidecar uses a separate queue to avoid blocking main responses. Use a persistent message queue (Redis, SQS) for production to survive process restarts.

---

### Option 4: Preference Summary Injection with Prompt Caching

Store the preference block as a cached system prompt prefix so the token cost is paid only once across all session calls.

```python
import json
from pathlib import Path
import anthropic

client = anthropic.Anthropic()
PREF_FILE = Path(".cached_prefs.json")

def load_prefs(user_id: str) -> dict:
    if PREF_FILE.exists():
        return json.loads(PREF_FILE.read_text()).get(user_id, {})
    return {}

def format_prefs_block(prefs: dict) -> str:
    if not prefs:
        return ""
    lines = ["<user_preferences>"]
    for k, v in prefs.items():
        lines.append(f"  <{k}>{v}</{k}>")
    lines.append("</user_preferences>")
    return "\n".join(lines)

def create_cached_system(prefs: dict) -> list[dict]:
    """Build a system prompt list with the preference block marked for caching."""
    pref_block = format_prefs_block(prefs)
    base_instructions = (
        "You are a helpful assistant. Always follow the user preferences above exactly. "
        "Do not ask the user to repeat their preferences — they are already set."
    )
    return [
        {
            "type": "text",
            "text": pref_block + "\n\n" + base_instructions,
            "cache_control": {"type": "ephemeral"},
        }
    ]

def chat(user_id: str, user_message: str) -> str:
    prefs = load_prefs(user_id)
    system = create_cached_system(prefs) if prefs else "You are a helpful assistant."

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )

    # Log cache performance
    usage = response.usage
    print(
        f"Tokens — input: {usage.input_tokens}, "
        f"cache_read: {getattr(usage, 'cache_read_input_tokens', 0)}, "
        f"cache_write: {getattr(usage, 'cache_creation_input_tokens', 0)}"
    )
    return response.content[0].text

def save_pref(user_id: str, key: str, value: str) -> None:
    prefs = {}
    if PREF_FILE.exists():
        prefs = json.loads(PREF_FILE.read_text())
    prefs.setdefault(user_id, {})[key] = value
    PREF_FILE.write_text(json.dumps(prefs, indent=2))

# Setup: save some prefs from a previous session
save_pref("user_42", "response_format", "bullet points only")
save_pref("user_42", "expertise_level", "senior Python engineer")
save_pref("user_42", "tech_stack", "FastAPI, PostgreSQL, Redis")

# First call: cache write (~150 token preference block written to cache)
r1 = chat("user_42", "How do I implement rate limiting in FastAPI?")
print(r1[:200])

# Second call: cache hit (preference block served from cache at 90% discount)
r2 = chat("user_42", "What about database connection pooling?")
print(r2[:200])
```

**Expected Token Savings:** First call: preference block (~150 tokens) written to cache. All subsequent calls within 5 minutes: cache hit = 90% discount on those 150 tokens. For 10 calls/session: 1 × 150 + 9 × 15 = 285 tokens vs 10 × 150 = 1,500 tokens without caching. 81% reduction on preference overhead.
**Environment:** Requires `anthropic-beta: prompt-caching-2024-07-31` header. Cache TTL is 5 minutes (ephemeral). Preference block must be identical across calls to hit cache — avoid dynamic timestamps in the cached prefix.

---

### Option 5: Preference Ontology — Hierarchical Preference Tree

Model user preferences as a hierarchical tree (global → domain → task-specific) so more specific preferences override less specific ones.

```python
import json
from pathlib import Path
import anthropic

class PrefNode:
    def __init__(self, value: str | None = None):
        self.value = value
        self.children: dict[str, "PrefNode"] = {}

    def get(self, *path: str) -> str | None:
        """Get most specific matching preference, falling back up the tree."""
        node = self
        last_value = node.value
        for key in path:
            if key in node.children:
                node = node.children[key]
                if node.value is not None:
                    last_value = node.value
            else:
                break
        return last_value

    def set(self, *path_and_value: str) -> None:
        *path, value = path_and_value
        node = self
        for key in path:
            node.children.setdefault(key, PrefNode())
            node = node.children[key]
        node.value = value

    def to_dict(self) -> dict:
        result = {}
        if self.value is not None:
            result["_value"] = self.value
        for k, child in self.children.items():
            result[k] = child.to_dict()
        return result

    @classmethod
    def from_dict(cls, data: dict) -> "PrefNode":
        node = cls(value=data.get("_value"))
        for k, v in data.items():
            if k != "_value":
                node.children[k] = cls.from_dict(v)
        return node

PREF_FILE = Path(".pref_ontology.json")

def load_tree(user_id: str) -> PrefNode:
    if PREF_FILE.exists():
        data = json.loads(PREF_FILE.read_text()).get(user_id, {})
        return PrefNode.from_dict(data)
    root = PrefNode()
    root.set("format", "prose")           # global default
    root.set("length", "medium")
    return root

def save_tree(user_id: str, tree: PrefNode) -> None:
    existing = {}
    if PREF_FILE.exists():
        existing = json.loads(PREF_FILE.read_text())
    existing[user_id] = tree.to_dict()
    PREF_FILE.write_text(json.dumps(existing, indent=2))

def resolve_prefs_for_task(tree: PrefNode, domain: str, task: str) -> str:
    """Resolve preferences for a specific domain+task, using hierarchy."""
    fmt = tree.get("format", domain, task) or "prose"
    length = tree.get("length", domain, task) or "medium"
    examples = tree.get("examples", domain, task) or "yes"
    lines = [
        f"Response format for {domain}/{task}: {fmt}",
        f"Length preference: {length}",
        f"Include code examples: {examples}",
    ]
    return "\n".join(lines)

def run_session(user_id: str, domain: str, task: str, query: str) -> str:
    tree = load_tree(user_id)
    pref_context = resolve_prefs_for_task(tree, domain, task)

    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=f"You are a helpful assistant.\n\n{pref_context}",
        messages=[{"role": "user", "content": query}],
    )
    return response.content[0].text

# Setup: global preferences with domain-specific overrides
tree = load_tree("user_42")
tree.set("format", "bullets")            # global: always use bullets
tree.set("length", "short")             # global: be concise
tree.set("format", "code", "long")      # code domain: use long code examples
tree.set("length", "code", "detailed")  # code domain: detailed for code
tree.set("format", "summary", "prose")  # summary domain: prose is fine
save_tree("user_42", tree)

# Each call gets the right preferences for its context
r1 = run_session("user_42", "code", "explanation", "How does Python's GIL work?")
r2 = run_session("user_42", "summary", "report", "Summarise Q3 performance")
print("Code response:\n", r1[:150])
print("\nSummary response:\n", r2[:150])
```

**Expected Token Savings:** Ontology resolves to ~50-80 tokens of injected context per call. Prevents re-negotiation of preferences (2-3 turns × 300 tokens = 600-900 tokens) per session. More specific preferences prevent generic responses that need correction.
**Environment:** Hierarchical preferences are most valuable when users interact across multiple domains (coding, writing, analysis) with different format needs for each. Start simple (flat JSON, Option 1) and migrate to ontology when domain-specific overrides become frequent.

---

### Option 6: Cross-Session Preference Digest — Weekly Consolidation

Run a weekly job that consolidates all learned preferences across sessions into a clean digest, removing outdated or contradictory entries.

```python
import json
from pathlib import Path
from datetime import datetime, timedelta
import anthropic

SESSION_LOG_DIR = Path(".session_logs")
SESSION_LOG_DIR.mkdir(exist_ok=True)
DIGEST_FILE = Path(".preference_digest.json")

def log_session_preference(user_id: str, preference: str, timestamp: str | None = None) -> None:
    """Append a preference observation to the session log."""
    log_path = SESSION_LOG_DIR / f"{user_id}_log.jsonl"
    entry = {
        "preference": preference,
        "timestamp": timestamp or datetime.now().isoformat(),
    }
    with log_path.open("a") as f:
        f.write(json.dumps(entry) + "\n")

def load_recent_observations(user_id: str, days: int = 30) -> list[str]:
    log_path = SESSION_LOG_DIR / f"{user_id}_log.jsonl"
    if not log_path.exists():
        return []
    cutoff = datetime.now() - timedelta(days=days)
    observations = []
    for line in log_path.read_text().splitlines():
        entry = json.loads(line)
        ts = datetime.fromisoformat(entry["timestamp"])
        if ts > cutoff:
            observations.append(entry["preference"])
    return observations

def consolidate_preferences(user_id: str) -> dict:
    """Weekly job: consolidate all session logs into a clean preference digest."""
    observations = load_recent_observations(user_id, days=30)
    if not observations:
        return {}

    client = anthropic.Anthropic()
    obs_text = "\n".join(f"- {o}" for o in observations)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system="""Consolidate user preference observations into a clean, deduplicated preference profile.
Resolve contradictions by keeping the most recent/frequent preference.
Return a JSON object: {preference_key: value, ...}""",
        messages=[{
            "role": "user",
            "content": f"Observations from past 30 days:\n{obs_text}",
        }],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
    digest = json.loads(raw)

    existing = {}
    if DIGEST_FILE.exists():
        existing = json.loads(DIGEST_FILE.read_text())
    existing[user_id] = {"digest": digest, "consolidated_at": datetime.now().isoformat()}
    DIGEST_FILE.write_text(json.dumps(existing, indent=2))
    return digest

def get_session_context(user_id: str) -> str:
    if not DIGEST_FILE.exists():
        return ""
    data = json.loads(DIGEST_FILE.read_text()).get(user_id, {})
    digest = data.get("digest", {})
    if not digest:
        return ""
    lines = ["Persistent user preferences:"]
    for k, v in digest.items():
        lines.append(f"  {k}: {v}")
    return "\n".join(lines)

# Simulate 2 weeks of logged preferences
for day_offset in range(14):
    ts = (datetime.now() - timedelta(days=14 - day_offset)).isoformat()
    log_session_preference("user_42", "user prefers bullet point lists", ts)
    log_session_preference("user_42", "user is a senior Python engineer", ts)
    if day_offset > 7:
        log_session_preference("user_42", "user now prefers numbered lists over bullets", ts)

# Run weekly consolidation
digest = consolidate_preferences("user_42")
print(f"Consolidated digest: {digest}")

# Comparison table
"""
| Approach | Storage | Update Frequency | Cache-Friendly | Scale |
|---|---|---|---|---|
| Option 1: JSON store | File | Per-session | No | Single instance |
| Option 2: Typed schema | File/DB | Per-session | No | Multi-instance |
| Option 3: Async sidecar | File/Queue | Per-turn | No | Async only |
| Option 4: Prompt caching | File | Per-session | Yes | Any |
| Option 5: Ontology tree | File/DB | Per-session | No | Complex domains |
| Option 6: Weekly digest | File/DB | Weekly job | Yes | Long-running users |
"""

# Use in session
context = get_session_context("user_42")
client = anthropic.Anthropic()
response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=256,
    system=f"You are a helpful assistant.\n{context}",
    messages=[{"role": "user", "content": "How do I use asyncio in Python?"}],
)
print(response.content[0].text[:200])
```

**Expected Token Savings:** Weekly consolidation removes redundant/contradictory entries, keeping the injected preference block minimal (~80 tokens vs 300+ for raw accumulated logs). Stale preferences that would cause wrong behaviour are pruned, preventing correction turns (~600 tokens each).
**Environment:** Schedule `consolidate_preferences()` as a weekly cron job. For high-volume users, run consolidation after every 50 sessions instead. Store logs in append-only format for full audit trail.
