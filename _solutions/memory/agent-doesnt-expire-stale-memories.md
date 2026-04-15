---
layout: solution
title: "Agent doesn't expire stale memories"
category: memory
description: "Agent accumulates memories indefinitely with no TTL or freshness check. Outdated facts — old email addresses, cancelled plans, superseded preferences — persist alongside current ones and are retrieved with equal weight, causing the agent to act on wrong information."
tags: [memory, ttl, freshness, staleness, retrieval, cleanup]
---

## Symptom

The agent confidently uses an email address the user changed six months ago, references a project that was cancelled, or applies a preference the user explicitly reversed — because the original memory was never expired. The agent has no mechanism to distinguish "remembered yesterday" from "remembered two years ago".

## Root Cause

Memory writes are append-only with no expiry timestamp. The retrieval path has no staleness filter. When two contradictory memories exist (old email vs. new email), the agent either picks the first one it finds, the most recent one, or blends both — all incorrect. Without TTL or explicit invalidation, memory only grows, never cleans itself.

## Fix

Attach a timestamp to every memory write. At retrieval time, filter out entries older than a configurable TTL. For memories that must survive longer, require explicit refresh or confirmation rather than silent retention.

---

### Option 1 — TTL-stamped memory with expiry filter at retrieval

```python
import anthropic
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic(api_key="sk-live-...")


@dataclass
class Memory:
    key: str
    value: str
    created_at: float = field(default_factory=time.time)
    ttl_seconds: float = 7 * 24 * 3600   # default: 7 days

    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl_seconds

    def age_hours(self) -> float:
        return (time.time() - self.created_at) / 3600


class TTLMemoryStore:
    def __init__(self) -> None:
        self._store: dict[str, Memory] = {}

    def write(self, key: str, value: str, ttl_seconds: float = 7 * 24 * 3600) -> None:
        self._store[key] = Memory(key=key, value=value, ttl_seconds=ttl_seconds)

    def read(self, key: str) -> str | None:
        m = self._store.get(key)
        if m is None:
            return None
        if m.is_expired():
            del self._store[key]
            print(f"Memory '{key}' expired ({m.age_hours():.1f}h old) — evicted")
            return None
        return m.value

    def all_fresh(self) -> dict[str, str]:
        """Return all non-expired memories."""
        fresh = {k: m.value for k, m in list(self._store.items()) if not m.is_expired()}
        expired = len(self._store) - len(fresh)
        if expired:
            # Clean up expired entries
            self._store = {k: m for k, m in self._store.items() if not m.is_expired()}
        return fresh

    def inject_context(self) -> str:
        """Format non-expired memories for system prompt injection."""
        memories = self.all_fresh()
        if not memories:
            return ""
        lines = [f"  - {k}: {v}" for k, v in memories.items()]
        return "Remembered facts (current):\n" + "\n".join(lines)


store = TTLMemoryStore()

# Write memories with different TTLs
store.write("user_email", "alice@example.com", ttl_seconds=30 * 24 * 3600)    # 30 days
store.write("preferred_language", "Python", ttl_seconds=365 * 24 * 3600)      # 1 year
store.write("current_project", "Project Alpha", ttl_seconds=7 * 24 * 3600)    # 7 days


def run_agent(user_message: str) -> str:
    context = store.inject_context()
    system = f"You are a helpful assistant.{chr(10) + context if context else ''}"

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text
```

**Expected Token Savings:** Expired memories are never injected — each expired entry saves its token cost on every future call; a 20-entry store that decays to 5 saves ~75 % of memory injection tokens.
**Environment:** Any agent with a key-value memory store; TTL is the minimum viable freshness mechanism.

---

### Option 2 — Category-based TTL policies

```python
import anthropic
import time
from enum import Enum
from dataclasses import dataclass, field

client = anthropic.Anthropic(api_key="sk-live-...")


class MemoryCategory(Enum):
    CONTACT = "contact"          # email, phone — medium TTL
    PREFERENCE = "preference"    # likes/dislikes — long TTL
    TASK = "task"                # current work items — short TTL
    FACT = "fact"                # general facts — medium TTL
    SESSION = "session"          # in-session notes — very short TTL


TTL_BY_CATEGORY: dict[MemoryCategory, float] = {
    MemoryCategory.CONTACT: 30 * 24 * 3600,     # 30 days
    MemoryCategory.PREFERENCE: 365 * 24 * 3600,  # 1 year
    MemoryCategory.TASK: 3 * 24 * 3600,          # 3 days
    MemoryCategory.FACT: 14 * 24 * 3600,         # 2 weeks
    MemoryCategory.SESSION: 2 * 3600,             # 2 hours
}


@dataclass
class CategorizedMemory:
    key: str
    value: str
    category: MemoryCategory
    created_at: float = field(default_factory=time.time)
    refreshed_at: float = field(default_factory=time.time)

    def ttl(self) -> float:
        return TTL_BY_CATEGORY[self.category]

    def is_expired(self) -> bool:
        return time.time() - self.refreshed_at > self.ttl()

    def refresh(self, new_value: str) -> None:
        self.value = new_value
        self.refreshed_at = time.time()


class CategorizedMemoryStore:
    def __init__(self) -> None:
        self._store: dict[str, CategorizedMemory] = {}

    def write(self, key: str, value: str, category: MemoryCategory) -> None:
        if key in self._store:
            self._store[key].refresh(value)
            print(f"Memory '{key}' refreshed (category={category.value})")
        else:
            self._store[key] = CategorizedMemory(key=key, value=value, category=category)

    def get_context(self) -> str:
        groups: dict[str, list[str]] = {}
        for key, m in list(self._store.items()):
            if m.is_expired():
                del self._store[key]
                print(f"Expired [{m.category.value}]: {key}")
                continue
            cat = m.category.value
            groups.setdefault(cat, []).append(f"{key}: {m.value}")

        if not groups:
            return ""
        lines = ["Known context:"]
        for cat, entries in groups.items():
            lines.append(f"  [{cat}]")
            lines.extend(f"    {e}" for e in entries)
        return "\n".join(lines)


mem = CategorizedMemoryStore()
mem.write("user_name", "Alice", MemoryCategory.CONTACT)
mem.write("preferred_tone", "concise", MemoryCategory.PREFERENCE)
mem.write("current_task", "writing unit tests", MemoryCategory.TASK)


def run_agent(user_message: str) -> str:
    context = mem.get_context()
    system = f"You are a helpful assistant.\n\n{context}" if context else "You are a helpful assistant."

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text
```

**Expected Token Savings:** Short-lived task memories expire quickly and stop appearing in context; long-lived preference memories persist but are few in number; total injection size stays bounded.
**Environment:** Agents with heterogeneous memory types; category-based TTL lets you tune expiry per type without per-entry configuration.

---

### Option 3 — Explicit confirmation: re-validate old memories before use

```python
import anthropic
import time

client = anthropic.Anthropic(api_key="sk-live-...")

STALE_THRESHOLD_SECONDS = 7 * 24 * 3600   # 7 days
CONFIRM_THRESHOLD_SECONDS = 30 * 24 * 3600  # 30 days


class ConfirmationMemoryStore:
    def __init__(self) -> None:
        self._store: dict[str, dict] = {}

    def write(self, key: str, value: str) -> None:
        self._store[key] = {
            "value": value,
            "written_at": time.time(),
            "confirmed_at": time.time(),
        }

    def confirm(self, key: str) -> None:
        if key in self._store:
            self._store[key]["confirmed_at"] = time.time()

    def get_with_status(self, key: str) -> tuple[str | None, str]:
        """
        Returns (value, status) where status is:
        'fresh' — confirmed recently
        'stale' — not confirmed in a while, use with caution
        'expired' — too old to trust
        """
        entry = self._store.get(key)
        if entry is None:
            return None, "not_found"

        age = time.time() - entry["confirmed_at"]
        if age > CONFIRM_THRESHOLD_SECONDS:
            return entry["value"], "expired"
        if age > STALE_THRESHOLD_SECONDS:
            return entry["value"], "stale"
        return entry["value"], "fresh"

    def fresh_memories_for_context(self) -> dict[str, str]:
        """Only return memories that are fresh or at most stale (not expired)."""
        result = {}
        for key, entry in self._store.items():
            age = time.time() - entry["confirmed_at"]
            if age <= CONFIRM_THRESHOLD_SECONDS:
                result[key] = entry["value"]
        return result


conf_store = ConfirmationMemoryStore()
conf_store.write("user_email", "alice@newdomain.com")
conf_store.write("team_name", "Platform Engineering")


def run_agent(user_message: str) -> str:
    # Build context from fresh/stale (not expired) memories
    memories = conf_store.fresh_memories_for_context()
    context = "\n".join(f"- {k}: {v}" for k, v in memories.items())
    system = (
        "You are a helpful assistant.\n\n"
        f"Stored context (confirm with user if uncertain):\n{context}"
        if context else "You are a helpful assistant."
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text
```

**Expected Token Savings:** Expired memories are excluded from injection; stale memories trigger in-agent confirmation, preventing incorrect actions before they happen.
**Environment:** High-stakes agents (booking, finance, healthcare) where acting on stale contact or preference data has real-world consequences.

---

### Option 4 — LLM-assisted freshness check: ask the model if it needs confirmation

```python
import anthropic
import time
import json

client = anthropic.Anthropic(api_key="sk-live-...")


def assess_memory_freshness(memories: list[dict]) -> list[dict]:
    """
    Ask a cheap model which memories might be stale given the current date.
    Returns annotated memory list with 'likely_stale' flag.
    """
    if not memories:
        return memories

    today = time.strftime("%Y-%m-%d")
    memory_json = json.dumps(memories, indent=2)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=(
            f"Today is {today}. You assess whether stored memories are likely stale. "
            "For each memory, add 'likely_stale': true if the information type typically "
            "changes within the time since it was stored. Email addresses, project names, "
            "and task status are often stale after 30+ days. Preferences are stale after 1 year. "
            "Return the same JSON array with the 'likely_stale' field added to each item."
        ),
        messages=[{"role": "user", "content": f"Assess these memories:\n{memory_json}"}],
    )

    try:
        text = response.content[0].text.strip()
        # Extract JSON from response
        import re
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
    except Exception:
        pass
    return memories


# Example memory store
memories = [
    {"key": "user_email", "value": "alice@old.com", "stored_days_ago": 45},
    {"key": "preferred_language", "value": "Python", "stored_days_ago": 200},
    {"key": "current_project", "value": "Rewrite Q4", "stored_days_ago": 90},
]

assessed = assess_memory_freshness(memories)
fresh = [m for m in assessed if not m.get("likely_stale", False)]
stale = [m for m in assessed if m.get("likely_stale", False)]

print(f"Fresh: {[m['key'] for m in fresh]}")
print(f"Stale: {[m['key'] for m in stale]}")
```

**Expected Token Savings:** Haiku assessment costs ~100 tokens; prevents injecting stale memories that would cost tokens to inject AND cause incorrect agent behavior.
**Environment:** Agents without a fixed TTL policy; useful when memory staleness is context-dependent (a project name is stale after 3 months for some users, 1 year for others).

---

### Option 5 — Memory versioning: supersede rather than accumulate

```python
import anthropic
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic(api_key="sk-live-...")


@dataclass
class VersionedMemory:
    key: str
    value: str
    version: int
    created_at: float = field(default_factory=time.time)
    superseded: bool = False


class VersionedMemoryStore:
    """
    When a key is written again, the old version is marked superseded.
    Retrieval only returns the latest non-superseded version.
    Periodic cleanup removes old versions.
    """

    def __init__(self) -> None:
        self._versions: list[VersionedMemory] = []
        self._current_version: dict[str, int] = {}

    def write(self, key: str, value: str) -> None:
        # Supersede all previous versions of this key
        for m in self._versions:
            if m.key == key:
                m.superseded = True

        version = (self._current_version.get(key, 0)) + 1
        self._current_version[key] = version
        self._versions.append(VersionedMemory(key=key, value=value, version=version))
        print(f"Memory '{key}' → v{version}: {value[:60]}")

    def get_latest(self, key: str) -> str | None:
        candidates = [m for m in self._versions if m.key == key and not m.superseded]
        return candidates[-1].value if candidates else None

    def all_current(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for m in self._versions:
            if not m.superseded:
                result[m.key] = m.value
        return result

    def cleanup_superseded(self, keep_last_n: int = 2) -> int:
        """Remove old superseded versions, keeping only the most recent N per key."""
        by_key: dict[str, list[VersionedMemory]] = {}
        for m in self._versions:
            by_key.setdefault(m.key, []).append(m)

        to_keep = set()
        for key, versions in by_key.items():
            # Keep all current + last N superseded (for audit)
            current = [v for v in versions if not v.superseded]
            old = sorted([v for v in versions if v.superseded], key=lambda x: x.created_at)
            to_keep.update(id(v) for v in current)
            to_keep.update(id(v) for v in old[-keep_last_n:])

        before = len(self._versions)
        self._versions = [v for v in self._versions if id(v) in to_keep]
        removed = before - len(self._versions)
        return removed


vm = VersionedMemoryStore()
vm.write("user_email", "alice@old.com")
vm.write("user_email", "alice@new.com")   # supersedes the old one
vm.write("current_project", "Project Alpha")

print("\nCurrent memories:")
for k, v in vm.all_current().items():
    print(f"  {k}: {v}")
```

**Expected Token Savings:** Versioning prevents two contradictory values for the same key from both appearing in the injected context; the model always sees exactly one value per key.
**Environment:** Agents where users frequently update stored information (email address changes, project renames); versioning prevents silent accumulation of contradictory facts.

---

### Option 6 — Scheduled memory janitor: background cleanup task

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")


@dataclass
class Memory:
    key: str
    value: str
    ttl_seconds: float
    created_at: float = field(default_factory=time.time)


class JanitorMemoryStore:
    def __init__(self, cleanup_interval: float = 3600.0) -> None:
        self._store: dict[str, Memory] = {}
        self._cleanup_interval = cleanup_interval
        self._janitor_task: asyncio.Task | None = None

    async def start_janitor(self) -> None:
        self._janitor_task = asyncio.create_task(self._janitor_loop())

    async def stop_janitor(self) -> None:
        if self._janitor_task:
            self._janitor_task.cancel()

    async def _janitor_loop(self) -> None:
        while True:
            await asyncio.sleep(self._cleanup_interval)
            removed = self._evict_expired()
            if removed:
                print(f"[Janitor] Evicted {removed} expired memories")

    def _evict_expired(self) -> int:
        now = time.time()
        expired_keys = [
            k for k, m in self._store.items()
            if now - m.created_at > m.ttl_seconds
        ]
        for k in expired_keys:
            del self._store[k]
        return len(expired_keys)

    def write(self, key: str, value: str, ttl_seconds: float = 86400.0) -> None:
        self._store[key] = Memory(key=key, value=value, ttl_seconds=ttl_seconds)

    def inject_context(self) -> str:
        # Evict inline in case janitor hasn't run yet
        self._evict_expired()
        if not self._store:
            return ""
        lines = [f"- {k}: {v}" for k, v in self._store.items()]
        return "Current context:\n" + "\n".join(lines)


mem = JanitorMemoryStore(cleanup_interval=600.0)


async def run_agent_async(user_message: str) -> str:
    context = mem.inject_context()
    system = f"You are a helpful assistant.\n\n{context}" if context else "You are a helpful assistant."

    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


async def main() -> None:
    await mem.start_janitor()
    mem.write("session_note", "User is debugging auth flow", ttl_seconds=3600)
    mem.write("preference", "use TypeScript", ttl_seconds=365 * 24 * 3600)

    result = await run_agent_async("How should I structure my auth module?")
    print(result)
    await mem.stop_janitor()


# Comparison table
# | Option | Expiry Mechanism | Contradiction Handling | Extra Cost |
# |--------|-----------------|----------------------|------------|
# | 1 TTL timestamp | Per-entry TTL | Overwrites on same key | None |
# | 2 Category TTL | Category-level TTL | Overwrites on same key | None |
# | 3 Confirmation gate | Manual confirm | Stale flagging | None |
# | 4 LLM freshness | Haiku assessment | External judge | ~100 tok |
# | 5 Versioning | Supersede on write | Only latest shown | None |
# | 6 Background janitor | Async cleanup loop | Overwrites on same key | None |

asyncio.run(main())
```

**Expected Token Savings:** The janitor silently removes expired entries on a schedule; long-running agents (days or weeks) accumulate no stale context; the inline eviction in `inject_context()` acts as a safety net.
**Environment:** Long-lived async agents (daemons, persistent assistants) that accumulate memories over days or weeks; the janitor runs in the background without blocking request handling.
