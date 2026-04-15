---
layout: solution
title: "Agent doesn't merge contradictory memories"
category: memory
description: "When new information contradicts a stored memory, the agent appends the new fact without resolving the conflict. Both versions persist and the agent alternates between them unpredictably — giving different answers to the same question depending on which memory surfaces first."
tags: [memory, contradiction, conflict-resolution, consistency, hallucination, prompt-engineering]
---

## Symptom

The user told the agent "my budget is $500" in session 1. In session 4, they say "I got a raise — my budget is now $2000". The agent stores both facts. In session 5, it randomly uses $500 or $2000 depending on context injection order. The user corrects it. The agent apologizes and stores a third fact. Now three contradictory budgets exist and the problem compounds.

## Root Cause

The agent stores memories by append, not by merge. Each new fact is an independent record with no link to prior related facts. The retrieval step returns semantically similar memories without checking whether they contradict each other. No reconciliation logic runs on write or on read — the agent has no concept of "this new fact supersedes that old fact".

## Fix

Detect contradictions at write time. Before storing a new memory, retrieve semantically similar existing memories and check for conflicts. On conflict, resolve by recency (newer wins), confidence, or explicit user instruction — then either update the existing record or mark the old one superseded.

---

### Option 1 — Recency-wins: overwrite on contradiction detection

```python
import anthropic
from dataclasses import dataclass, field
import time

client = anthropic.Anthropic(api_key="sk-live-...")


@dataclass
class Memory:
    key: str          # semantic key, e.g. "user_budget"
    value: str        # the stored fact
    timestamp: float = field(default_factory=time.time)
    superseded: bool = False
    superseded_by: str | None = None   # key of the replacement memory


class MemoryStore:
    def __init__(self):
        self._memories: list[Memory] = []

    def get_active(self) -> list[Memory]:
        return [m for m in self._memories if not m.superseded]

    def find_related(self, key: str) -> list[Memory]:
        """Find active memories whose key overlaps with the new key."""
        key_parts = set(key.lower().split("_"))
        return [
            m for m in self.get_active()
            if key_parts & set(m.key.lower().split("_"))
        ]

    def add(self, key: str, value: str) -> str:
        """Add a memory, superseding any conflicting prior memories."""
        related = self.find_related(key)
        superseded_keys = []

        for old in related:
            if old.key == key or self._is_same_topic(old.key, key):
                old.superseded = True
                old.superseded_by = key
                superseded_keys.append(old.key)
                print(f"[Memory] Superseded '{old.key}': '{old.value}'")

        new_mem = Memory(key=key, value=value)
        self._memories.append(new_mem)

        if superseded_keys:
            return f"Updated (superseded {superseded_keys}): {key} = {value}"
        return f"Added: {key} = {value}"

    def _is_same_topic(self, key1: str, key2: str) -> bool:
        """Check if two keys refer to the same attribute."""
        parts1 = set(key1.lower().split("_"))
        parts2 = set(key2.lower().split("_"))
        overlap = len(parts1 & parts2) / max(len(parts1 | parts2), 1)
        return overlap >= 0.5

    def format_for_prompt(self) -> str:
        active = self.get_active()
        if not active:
            return "No stored memories."
        return "\n".join(f"- {m.key}: {m.value}" for m in active)


store = MemoryStore()

MEMORY_EXTRACT_SYSTEM = (
    "Extract a factual claim from the user message as a key-value pair. "
    "Key: snake_case topic (e.g. 'user_budget', 'user_location', 'preferred_language'). "
    "Value: the stated fact. "
    "Reply with exactly: KEY: <key>\nVALUE: <value>\n"
    "If no new factual claim, reply: NONE"
)


def maybe_store_memory(user_message: str):
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        system=MEMORY_EXTRACT_SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    text = response.content[0].text.strip()
    if text == "NONE" or "KEY:" not in text:
        return
    lines = {line.split(":")[0].strip(): ":".join(line.split(":")[1:]).strip()
             for line in text.splitlines() if ":" in line}
    key = lines.get("KEY", "").strip()
    value = lines.get("VALUE", "").strip()
    if key and value:
        result = store.add(key, value)
        print(f"[Memory store] {result}")


def run_agent(user_message: str) -> str:
    maybe_store_memory(user_message)
    memory_block = store.format_for_prompt()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        system=f"You are a helpful assistant.\n\nKnown user facts:\n{memory_block}",
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


# Demonstrate contradiction resolution
print(run_agent("My budget for this project is $500."))
print(run_agent("I just got a raise — my budget is now $2000."))
print(run_agent("What's my budget?"))
# Should answer $2000, not $500
```

**Expected Token Savings:** Eliminates contradictory context that causes the model to hedge or give conflicting answers — prevents the follow-up clarification turns (2–3 × ~300 tokens each) needed to resolve user confusion.
**Environment:** Personalization agents, preference stores, any agent that accumulates user facts across sessions; recency-wins is the correct default for user-stated preferences.

---

### Option 2 — LLM contradiction detector with explicit resolution

```python
import anthropic
from dataclasses import dataclass, field
import time

client = anthropic.Anthropic(api_key="sk-live-...")


@dataclass
class Memory:
    id: int
    content: str
    timestamp: float = field(default_factory=time.time)
    active: bool = True


class SmartMemoryStore:
    def __init__(self):
        self._memories: list[Memory] = []
        self._next_id = 0

    CONTRADICTION_CHECK_SYSTEM = (
        "You are a memory consistency checker. Given an existing memory and a new statement, "
        "determine if they contradict each other.\n"
        "Reply with exactly one of:\n"
        "  CONTRADICTS: <brief reason>\n"
        "  COMPATIBLE\n"
        "  UPDATES: <brief reason> (new info refines without fully contradicting)"
    )

    def _check_contradiction(self, existing: str, new_fact: str) -> str:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            system=self.CONTRADICTION_CHECK_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"Existing: {existing}\nNew: {new_fact}",
            }],
        )
        return response.content[0].text.strip()

    def add(self, new_fact: str) -> None:
        """Add new_fact, resolving contradictions against active memories."""
        to_supersede = []
        active = [m for m in self._memories if m.active]

        for mem in active:
            verdict = self._check_contradiction(mem.content, new_fact)
            if verdict.startswith("CONTRADICTS") or verdict.startswith("UPDATES"):
                print(f"[Memory] {verdict} — superseding: '{mem.content}'")
                to_supersede.append(mem.id)

        for mid in to_supersede:
            for m in self._memories:
                if m.id == mid:
                    m.active = False

        new_mem = Memory(id=self._next_id, content=new_fact)
        self._next_id += 1
        self._memories.append(new_mem)
        print(f"[Memory] Stored: '{new_fact}'")

    def get_context(self) -> str:
        active = [m for m in self._memories if m.active]
        if not active:
            return "No stored context."
        return "\n".join(f"- {m.content}" for m in active)


store = SmartMemoryStore()


def run_agent(user_message: str) -> str:
    # Detect new facts and store with contradiction resolution
    facts_to_store = [user_message]  # simplified — in practice, extract claims
    for fact in facts_to_store:
        store.add(fact)

    context = store.get_context()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        system=f"You are a helpful assistant.\n\nContext:\n{context}",
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


# Test
run_agent("I prefer Python for backend work.")
run_agent("Actually I've switched to Go for all new backend projects.")
run_agent("What language should I use for a new microservice?")
```

**Expected Token Savings:** Haiku contradiction check costs ~30 tokens per memory pair; prevents a growing list of contradictory memories that bloat the context window with ~50 tokens per stale fact.
**Environment:** Agents with a growing memory store; LLM-based detection catches semantic contradictions that keyword matching would miss ("prefer Python" vs "switched to Go").

---

### Option 3 — Structured memory with explicit field update

```python
import anthropic
from dataclasses import dataclass, asdict
import json

client = anthropic.Anthropic(api_key="sk-live-...")


@dataclass
class UserProfile:
    """Structured profile — each field has a single authoritative value."""
    preferred_language: str | None = None
    budget_usd: int | None = None
    location: str | None = None
    communication_style: str | None = None  # formal | casual | technical
    experience_level: str | None = None      # beginner | intermediate | expert
    timezone: str | None = None

    def update_from_dict(self, updates: dict) -> list[str]:
        """Apply updates, return list of changed fields."""
        changed = []
        for field, new_val in updates.items():
            if hasattr(self, field) and new_val is not None:
                old_val = getattr(self, field)
                if old_val != new_val:
                    setattr(self, field, new_val)
                    changed.append(f"{field}: {old_val!r} → {new_val!r}")
        return changed

    def to_prompt(self) -> str:
        fields = {k: v for k, v in asdict(self).items() if v is not None}
        if not fields:
            return "No user profile data."
        return "\n".join(f"  {k}: {v}" for k, v in fields.items())


profile = UserProfile()

EXTRACT_SYSTEM = (
    "Extract user profile updates from the message. "
    "Return a JSON object with only the fields that are explicitly stated. "
    "Valid fields: preferred_language, budget_usd (integer), location, "
    "communication_style (formal/casual/technical), experience_level "
    "(beginner/intermediate/expert), timezone (IANA format). "
    "Return {} if no profile information is present."
)


def update_profile_from_message(message: str) -> list[str]:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=EXTRACT_SYSTEM,
        messages=[{"role": "user", "content": message}],
    )
    try:
        updates = json.loads(response.content[0].text.strip())
        if updates:
            changes = profile.update_from_dict(updates)
            for c in changes:
                print(f"[Profile] Updated: {c}")
            return changes
    except json.JSONDecodeError:
        pass
    return []


def run_agent(user_message: str) -> str:
    update_profile_from_message(user_message)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        system=f"You are a helpful assistant.\n\nUser profile:\n{profile.to_prompt()}",
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


# Profile updates are idempotent — no contradiction possible
run_agent("I'm a Python developer with intermediate experience.")
run_agent("I've leveled up — I'm now an expert Python developer.")
run_agent("My budget for tools is $200/month.")
run_agent("I just got a budget increase to $500/month.")
print(f"\nFinal profile:\n{profile.to_prompt()}")
# Should show: experience_level=expert, budget_usd=500
```

**Expected Token Savings:** Structured profile eliminates all contradiction by design — each field is a single slot, not an append list; profile context stays at a fixed ~100–200 tokens regardless of how many times the user updates their preferences.
**Environment:** Personalization agents with predictable profile fields; schema-based storage is the most reliable approach when the memory domain is well-defined.

---

### Option 4 — Version-aware memory with history log

```python
import anthropic
from dataclasses import dataclass, field
import time

client = anthropic.Anthropic(api_key="sk-live-...")


@dataclass
class MemoryVersion:
    value: str
    timestamp: float
    source: str   # "user_stated" | "inferred" | "corrected"


@dataclass
class VersionedMemory:
    key: str
    versions: list[MemoryVersion] = field(default_factory=list)

    @property
    def current(self) -> MemoryVersion | None:
        return self.versions[-1] if self.versions else None

    def add_version(self, value: str, source: str = "user_stated"):
        self.versions.append(MemoryVersion(
            value=value,
            timestamp=time.time(),
            source=source,
        ))

    def history_summary(self) -> str:
        if len(self.versions) <= 1:
            return ""
        prev = self.versions[-2]
        curr = self.versions[-1]
        return f"(changed from '{prev.value}' on {time.ctime(prev.timestamp)[:10]})"


class VersionedMemoryStore:
    def __init__(self):
        self._store: dict[str, VersionedMemory] = {}

    def upsert(self, key: str, value: str, source: str = "user_stated"):
        if key not in self._store:
            self._store[key] = VersionedMemory(key=key)
        mem = self._store[key]
        if mem.current and mem.current.value == value:
            return   # no change
        if mem.current:
            print(f"[VersionedMemory] '{key}' updated: '{mem.current.value}' → '{value}'")
        else:
            print(f"[VersionedMemory] '{key}' created: '{value}'")
        mem.add_version(value, source)

    def get_prompt_context(self, include_history: bool = False) -> str:
        lines = []
        for key, mem in self._store.items():
            if not mem.current:
                continue
            line = f"- {key}: {mem.current.value}"
            if include_history:
                hist = mem.history_summary()
                if hist:
                    line += f" {hist}"
            lines.append(line)
        return "\n".join(lines) if lines else "No stored facts."

    def get_version_history(self, key: str) -> list[str]:
        if key not in self._store:
            return []
        return [f"{v.value} ({v.source}, {time.ctime(v.timestamp)[:10]})"
                for v in self._store[key].versions]


store = VersionedMemoryStore()


def run_agent(user_message: str, extract_facts: bool = True) -> str:
    if extract_facts:
        # In practice, use Haiku to extract key-value pairs from the message
        # Here we simulate direct upserts for clarity
        pass

    context = store.get_prompt_context(include_history=True)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        system=f"You are a helpful assistant.\n\nUser context:\n{context}",
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


# Simulate preference evolution
store.upsert("editor", "VS Code")
store.upsert("editor", "Neovim")   # update — no contradiction confusion
store.upsert("os", "macOS")
store.upsert("os", "Linux")        # update

print(store.get_prompt_context(include_history=True))
print("\nEditor history:", store.get_version_history("editor"))
```

**Expected Token Savings:** Version history lets the agent acknowledge changes explicitly ("you switched from VS Code to Neovim") without storing both as active facts; the prompt context stays at one fact per key regardless of history depth.
**Environment:** Agents where users frequently update preferences and expect the agent to remember the change, not the original value; the history log supports "why did you recommend X?" questions.

---

### Option 5 — Contradiction audit: surface conflicts before answering

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")

# In-memory store simulating a larger persistent store
MEMORIES: list[dict] = []


def add_memory(content: str):
    MEMORIES.append({"id": len(MEMORIES), "content": content, "active": True})


CONFLICT_AUDIT_SYSTEM = (
    "Given a list of stored memories, identify any contradictory pairs. "
    "A contradiction is when two memories assert incompatible facts about the same subject. "
    "Return a JSON list of objects: [{\"mem_id_a\": int, \"mem_id_b\": int, \"reason\": str}]. "
    "Return [] if no contradictions found."
)


def audit_for_contradictions() -> list[dict]:
    active = [m for m in MEMORIES if m["active"]]
    if len(active) < 2:
        return []
    mem_list = "\n".join(f"[{m['id']}] {m['content']}" for m in active)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=CONFLICT_AUDIT_SYSTEM,
        messages=[{"role": "user", "content": mem_list}],
    )
    try:
        return json.loads(response.content[0].text.strip())
    except json.JSONDecodeError:
        return []


def resolve_contradiction(mem_a: dict, mem_b: dict) -> int:
    """Return the ID of the memory to keep (newer wins)."""
    return mem_b["id"]   # simple: keep the most recently added


def run_agent_with_audit(user_message: str) -> str:
    # Audit for contradictions before answering
    conflicts = audit_for_contradictions()
    if conflicts:
        print(f"[Audit] Found {len(conflicts)} contradiction(s)")
        for conflict in conflicts:
            a = next((m for m in MEMORIES if m["id"] == conflict["mem_id_a"]), None)
            b = next((m for m in MEMORIES if m["id"] == conflict["mem_id_b"]), None)
            if a and b:
                keep_id = resolve_contradiction(a, b)
                drop_id = conflict["mem_id_a"] if keep_id == conflict["mem_id_b"] else conflict["mem_id_b"]
                for m in MEMORIES:
                    if m["id"] == drop_id:
                        m["active"] = False
                        print(f"[Audit] Deactivated [{drop_id}]: '{m['content']}' — reason: {conflict['reason']}")

    active = [m for m in MEMORIES if m["active"]]
    context = "\n".join(f"- {m['content']}" for m in active)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        system=f"You are a helpful assistant.\n\nKnown facts:\n{context or 'None'}",
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


# Simulate contradictions building up
add_memory("User prefers Python for all scripting tasks")
add_memory("User prefers Go for all scripting tasks")  # contradicts previous
add_memory("User is located in San Francisco")
add_memory("User is located in New York")              # contradicts previous

print(run_agent_with_audit("What's the best language for a quick script?"))
```

**Expected Token Savings:** Audit runs at O(N) memory pairs using cheap Haiku model (~50 tokens per pair); resolves contradictions before they reach the main model context, preventing the main model from hedging between conflicting facts.
**Environment:** Agents with accumulated memories across many sessions; audit-on-read is appropriate when write-time conflict detection wasn't applied to legacy memories.

---

### Option 6 — Consensus merge: blend compatible facts, resolve incompatible ones

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")


class ConsensusMemoryStore:
    """
    Attempts to merge new facts with existing ones using three strategies:
    - MERGE: new fact adds detail to existing fact (both partially true)
    - SUPERSEDE: new fact replaces old fact (they are incompatible)
    - APPEND: new fact is independent of existing facts
    """

    MERGE_SYSTEM = (
        "You are a memory merging assistant. Given an existing memory and a new statement:\n"
        "1. Determine the relationship: SUPERSEDE, MERGE, or APPEND\n"
        "2. If SUPERSEDE: the new fact replaces the old one\n"
        "3. If MERGE: combine both into a single accurate statement\n"
        "4. If APPEND: keep both independently\n"
        "Reply as JSON: {\"action\": \"SUPERSEDE|MERGE|APPEND\", \"merged_content\": \"...(for MERGE only)\"}"
    )

    def __init__(self):
        self._memories: list[dict] = []

    def add(self, new_fact: str):
        if not self._memories:
            self._memories.append({"content": new_fact, "active": True})
            return

        # Check against each active memory
        for mem in self._memories:
            if not mem["active"]:
                continue
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                system=self.MERGE_SYSTEM,
                messages=[{
                    "role": "user",
                    "content": f"Existing: {mem['content']}\nNew: {new_fact}",
                }],
            )
            try:
                result = json.loads(response.content[0].text.strip())
            except json.JSONDecodeError:
                continue

            action = result.get("action", "APPEND")

            if action == "SUPERSEDE":
                mem["active"] = False
                print(f"[Consensus] SUPERSEDE: '{mem['content']}' → '{new_fact}'")
                self._memories.append({"content": new_fact, "active": True})
                return

            elif action == "MERGE":
                merged = result.get("merged_content", new_fact)
                mem["active"] = False
                self._memories.append({"content": merged, "active": True})
                print(f"[Consensus] MERGE → '{merged}'")
                return

        # APPEND (no conflicts found)
        self._memories.append({"content": new_fact, "active": True})
        print(f"[Consensus] APPEND: '{new_fact}'")

    def get_context(self) -> str:
        active = [m for m in self._memories if m["active"]]
        return "\n".join(f"- {m['content']}" for m in active) or "No memories."


store = ConsensusMemoryStore()


def run_agent(user_message: str) -> str:
    store.add(user_message)  # simplified — in practice, extract claim first
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        system=f"You are a helpful assistant.\n\nContext:\n{store.get_context()}",
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


# Comparison table
# | Option | Conflict Detection | Resolution Strategy | Extra Cost |
# |--------|-------------------|--------------------|-----------:|
# | 1 Recency-wins | Key overlap | Newer supersedes | ~0 tok |
# | 2 LLM detector | Semantic check | Explicit supersede | ~30 tok/pair |
# | 3 Structured fields | Schema-enforced | Field overwrite | ~0 tok |
# | 4 Versioned | History log | Newest version active | ~0 tok |
# | 5 Audit on read | Batch conflict scan | Deactivate older | ~50 tok/scan |
# | 6 Consensus merge | Per-pair LLM check | Merge or supersede | ~50 tok/add |

store.add("I know Python and JavaScript fairly well.")
store.add("I'm an expert in Python now — just passed the certification.")
print(store.get_context())
# Should reflect expert Python status, not "fairly well"
```

**Expected Token Savings:** Consensus merge collapses 2 related memories into 1, keeping context size bounded; prevents the unbounded context growth that would occur if every preference update doubled the memory entries for that topic.
**Environment:** Agents where partial updates are common (skill level updates, location refinements, budget adjustments); merge is more expressive than supersede when new information adds nuance rather than replacing entirely.
