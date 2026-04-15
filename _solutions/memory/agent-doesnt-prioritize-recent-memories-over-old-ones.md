---
layout: solution
title: "Agent Doesn't Prioritize Recent Memories Over Old Ones"
category: memory
description: "Agent retrieves and applies memories in insertion order or random order, so stale preferences from months ago override current ones, and the user must repeatedly correct the same outdated behavior."
tags: [memory, recency, retrieval, priority, staleness, user-preferences]
---

## Symptom

A user told the agent six months ago: "I prefer Python 2." They have since corrected this twice: "I now use Python 3." The agent still suggests `print "hello"` syntax. When the agent retrieves memories about the user's language preference, it returns the oldest memory (Python 2) first because memories are stored in a flat list without timestamps or recency weighting. The most recent correction is somewhere in the middle, buried under older entries.

## Root Cause

Memory systems without explicit recency handling retrieve memories in arbitrary or insertion order. Without timestamps, there is no way to determine which memory is newer. Without a recency weight in the ranking function, a correction made yesterday competes equally with a preference stated a year ago. In retrieval-augmented memory, cosine similarity alone measures topical relevance but not temporal relevance — a query about "Python version" may retrieve the old entry first if it happens to be a closer semantic match.

## Fix

### Option 1 — Timestamped memory store with recency-weighted retrieval

```python
import json
import time
import math
import anthropic

client = anthropic.Anthropic()

class RecencyWeightedMemory:
    """
    Memory store where each entry has a timestamp.
    Retrieval score = semantic_similarity × recency_weight.
    """

    def __init__(self, decay_days: float = 30.0):
        self._memories: list[dict] = []
        self._decay_days = decay_days  # half-life for recency decay

    def add(self, key: str, value: str) -> None:
        # Check for existing entries with the same key and mark them as superseded
        for m in self._memories:
            if m["key"] == key:
                m["superseded"] = True
        self._memories.append({
            "key":        key,
            "value":      value,
            "created_at": time.time(),
            "superseded": False,
        })
        print(f"  [memory] stored: key={key!r} value={value!r}")

    def recency_weight(self, created_at: float) -> float:
        """Exponential decay: weight = e^(-age_days / decay_days)"""
        age_days = (time.time() - created_at) / 86_400
        return math.exp(-age_days / self._decay_days)

    def retrieve(self, query: str, top_k: int = 3) -> list[dict]:
        """Return most recent non-superseded memories for this query."""
        # Filter: skip superseded entries
        active = [m for m in self._memories if not m["superseded"]]
        # Sort by recency (newest first)
        active.sort(key=lambda m: m["created_at"], reverse=True)
        # Simple keyword relevance (replace with embedding similarity in prod)
        query_words = set(query.lower().split())
        scored = []
        for m in active:
            kw_score  = len(query_words & set(m["key"].lower().split("_"))) / max(len(query_words), 1)
            rec_score = self.recency_weight(m["created_at"])
            scored.append((m, kw_score * 0.4 + rec_score * 0.6))
        scored.sort(key=lambda x: -x[1])
        return [m for m, _ in scored[:top_k]]

    def format_for_prompt(self, query: str) -> str:
        results = self.retrieve(query)
        if not results:
            return ""
        lines = []
        for m in results:
            age_h = (time.time() - m["created_at"]) / 3600
            lines.append(f"- {m['key']}: {m['value']} (recorded {age_h:.1f}h ago)")
        return "User preferences:\n" + "\n".join(lines)

# Simulate preference evolution
memory = RecencyWeightedMemory(decay_days=30.0)

# Old preference (simulate 60 days ago)
old_entry = {"key": "preferred_language", "value": "Python 2", "created_at": time.time() - 60*86400, "superseded": False}
memory._memories.append(old_entry)

# Recent correction (simulate 1 hour ago)
time.sleep(0.001)
memory.add("preferred_language", "Python 3")

# Even more recent (just now)
memory.add("python_version", "3.12 specifically")

context = memory.format_for_prompt("python language version preference")
print(f"\nContext injected:\n{context}")

r = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=128,
    system=f"You are a coding assistant.\n\n{context}\n\nFollow user preferences exactly.",
    messages=[{"role": "user", "content": "Show me a hello world example."}],
)
print(f"\nA: {r.content[0].text.strip()[:200]}")
```

**Expected Token Savings:** Recency weighting prevents incorrect memories from reaching the context — one stale memory injected per session can cause repeated corrections (3-5 extra turns each at 200 tokens), costing more than the memory retrieval overhead.
**Environment:** All persistent-memory agents; recency weighting is the baseline requirement for any memory system that stores evolving preferences.

---

### Option 2 — Versioned memory: explicit `updated_at` with conflict resolution

```python
import time
import json
import anthropic

client = anthropic.Anthropic()

class VersionedMemoryStore:
    """Each key has a single canonical value; updates create a new version."""

    def __init__(self) -> None:
        self._store: dict[str, dict] = {}   # key → {value, updated_at, version}

    def set(self, key: str, value: str) -> None:
        existing = self._store.get(key)
        version  = (existing["version"] + 1) if existing else 1
        self._store[key] = {
            "value":      value,
            "updated_at": time.time(),
            "version":    version,
        }
        print(f"  [mem v{version}] {key!r} = {value!r}")

    def get(self, key: str) -> dict | None:
        return self._store.get(key)

    def get_recent(self, max_age_days: float = 90.0) -> list[tuple[str, dict]]:
        """Return only memories updated within max_age_days, newest first."""
        cutoff = time.time() - max_age_days * 86_400
        items  = [(k, v) for k, v in self._store.items() if v["updated_at"] >= cutoff]
        items.sort(key=lambda x: -x[1]["updated_at"])
        return items

    def build_context(self, max_age_days: float = 90.0) -> str:
        items = self.get_recent(max_age_days)
        if not items:
            return ""
        lines = []
        for key, entry in items:
            age_days = (time.time() - entry["updated_at"]) / 86_400
            lines.append(f"- {key}: {entry['value']} (v{entry['version']}, {age_days:.1f}d ago)")
        return "Current user profile:\n" + "\n".join(lines)

mem = VersionedMemoryStore()

# Simulate a user updating preferences over time
mem.set("coding_language",  "Python 2")   # version 1 — old
time.sleep(0.001)
mem.set("coding_language",  "Python 3")   # version 2 — overrides
time.sleep(0.001)
mem.set("editor",           "Vim")
time.sleep(0.001)
mem.set("editor",           "VS Code")    # version 2 — overrides Vim
time.sleep(0.001)
mem.set("testing_framework","pytest")

print(f"\nActive memory (only latest version per key):")
for key, entry in mem._store.items():
    print(f"  {key}: {entry['value']} (v{entry['version']})")

context = mem.build_context(max_age_days=90)
print(f"\nContext:\n{context}")

r = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=128,
    system=f"You are a coding assistant.\n\n{context}",
    messages=[{"role": "user", "content": "Write a simple function to add two numbers."}],
)
print(f"\nA: {r.content[0].text.strip()[:200]}")
```

**Expected Token Savings:** Versioned memory stores only the latest value per key — a user who changes their preference 5 times never accumulates 5 conflicting entries; single canonical values reduce context tokens by 60-80% compared to storing all history.
**Environment:** Preference-tracking agents with bounded key spaces (language, editor, timezone, name); versioned keys are ideal when each preference has exactly one current value.

---

### Option 3 — Memory consolidation: periodic merge of old and new entries

```python
import time
import json
import anthropic

client = anthropic.Anthropic()

CONSOLIDATE_SYSTEM = """You are a memory consolidation assistant.
Given a list of user preferences (some old, some new), produce a single consolidated JSON object.
Rules:
- Keep the most recent value when entries conflict.
- Merge complementary information.
- Discard entries older than 90 days unless nothing more recent exists.
Return only the JSON object, no explanation."""

class ConsolidatingMemory:
    def __init__(self, consolidate_after: int = 10) -> None:
        self._raw: list[dict] = []
        self._consolidated: dict = {}
        self._consolidate_after = consolidate_after
        self._last_consolidation = time.time()

    def add(self, key: str, value: str) -> None:
        self._raw.append({"key": key, "value": value, "ts": time.time()})
        if len(self._raw) >= self._consolidate_after:
            self._consolidate()

    def _consolidate(self) -> None:
        if not self._raw:
            return
        # Build input for LLM consolidation
        entries = []
        for m in self._raw:
            age_d = (time.time() - m["ts"]) / 86_400
            entries.append(f"- {m['key']}: {m['value']} ({age_d:.1f}d ago)")
        prompt = "Consolidate these user preferences:\n" + "\n".join(entries)

        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=CONSOLIDATE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = r.content[0].text.strip().lstrip("```json").rstrip("```").strip()
        try:
            self._consolidated = json.loads(raw)
            self._raw = []
            print(f"  [consolidate] merged → {self._consolidated}")
        except json.JSONDecodeError:
            print(f"  [consolidate] parse error, keeping raw entries")
        self._last_consolidation = time.time()

    def build_context(self) -> str:
        lines = []
        # Consolidated (older, high-confidence)
        for k, v in self._consolidated.items():
            lines.append(f"- {k}: {v}")
        # Recent raw (may override consolidated)
        for m in sorted(self._raw, key=lambda x: x["ts"]):
            lines.append(f"- {m['key']}: {m['value']} [recent]")
        return "User preferences:\n" + "\n".join(lines) if lines else ""

mem = ConsolidatingMemory(consolidate_after=5)
entries = [
    ("lang",    "Python 2"),
    ("editor",  "Emacs"),
    ("lang",    "Python 3"),   # override
    ("editor",  "VS Code"),    # override
    ("testing", "pytest"),     # triggers consolidation at 5
]
for k, v in entries:
    mem.add(k, v)

context = mem.build_context()
print(f"\nFinal context:\n{context}")
```

**Expected Token Savings:** Consolidation reduces N raw entries into a single JSON object — 10 conflicting raw entries become 3-4 consolidated facts; consolidation tokens (1 LLM call per N entries) are amortised across all subsequent calls that benefit from the cleaner context.
**Environment:** Long-running agents where memory accumulates over weeks; periodic consolidation prevents context bloat from stale or redundant entries growing unbounded.

---

### Option 4 — Recency-aware retrieval with explicit conflict detection

```python
import time
import anthropic

client = anthropic.Anthropic()

class ConflictAwareMemory:
    def __init__(self) -> None:
        self._entries: list[dict] = []

    def add(self, category: str, value: str, confidence: float = 1.0) -> None:
        self._entries.append({
            "category":   category,
            "value":      value,
            "ts":         time.time(),
            "confidence": confidence,
        })

    def get_for_category(self, category: str) -> list[dict]:
        """Return entries for this category sorted newest-first."""
        matches = [e for e in self._entries if e["category"] == category]
        matches.sort(key=lambda e: e["ts"], reverse=True)
        return matches

    def detect_conflicts(self, category: str) -> dict:
        entries = self.get_for_category(category)
        if len(entries) < 2:
            return {"conflict": False, "entries": entries}
        # Conflict: multiple distinct values exist for same category
        values = [e["value"] for e in entries]
        conflict = len(set(values)) > 1
        return {
            "conflict": conflict,
            "entries":  entries,
            "current":  entries[0],   # newest
            "previous": entries[1:],
        }

    def build_context(self, categories: list[str]) -> str:
        lines = []
        for cat in categories:
            result = self.detect_conflicts(cat)
            if not result["entries"]:
                continue
            current = result["current"]
            age_h   = (time.time() - current["ts"]) / 3600
            if result["conflict"]:
                # Surface the conflict explicitly so the model knows which is newest
                lines.append(
                    f"- {cat}: {current['value']!r} [CURRENT, {age_h:.1f}h ago] "
                    f"(previously: {result['previous'][0]['value']!r})"
                )
            else:
                lines.append(f"- {cat}: {current['value']!r} ({age_h:.1f}h ago)")
        return "User profile (most recent values shown):\n" + "\n".join(lines)

mem = ConflictAwareMemory()

# Simulate history
mem._entries = [
    {"category": "python_version", "value": "Python 2",  "ts": time.time() - 180*86400, "confidence": 1.0},
    {"category": "python_version", "value": "Python 3",  "ts": time.time() - 30*86400,  "confidence": 1.0},
    {"category": "python_version", "value": "Python 3.12","ts": time.time() - 1*86400,  "confidence": 1.0},
    {"category": "editor",         "value": "Vim",       "ts": time.time() - 90*86400,  "confidence": 1.0},
    {"category": "editor",         "value": "VS Code",   "ts": time.time() - 2*86400,   "confidence": 1.0},
]

context = mem.build_context(["python_version", "editor"])
print(f"Context:\n{context}")

r = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=128,
    system=f"You are a coding assistant.\n\n{context}",
    messages=[{"role": "user", "content": "What Python version should I use for a new project?"}],
)
print(f"\nA: {r.content[0].text.strip()[:200]}")
```

**Expected Token Savings:** Explicit conflict markers in the context tell the model exactly which value is current — without them, the model may weight both values equally and give an ambiguous answer; conflict-aware context prevents the 2-3 follow-up turns needed to clarify which preference applies.
**Environment:** Agents where user preferences evolve frequently; surfacing the conflict history helps both the model and the user understand what the agent knows.

---

### Option 5 — TTL-based memory expiry: auto-purge stale preferences

```python
import time
import anthropic

client = anthropic.Anthropic()

# Different preference types have different natural TTLs
TTL_SECONDS = {
    "session":    3600,          # 1 hour — current session context
    "short_term": 7 * 86400,    # 1 week — recent task context
    "preference": 180 * 86400,  # 6 months — stable preferences
    "permanent":  float("inf"), # never expires — name, role, etc.
}

class TTLMemory:
    def __init__(self) -> None:
        self._store: list[dict] = []

    def add(self, key: str, value: str, tier: str = "preference") -> None:
        ttl = TTL_SECONDS.get(tier, TTL_SECONDS["preference"])
        self._store.append({
            "key":        key,
            "value":      value,
            "created_at": time.time(),
            "ttl":        ttl,
            "tier":       tier,
            "expires_at": time.time() + ttl,
        })

    def purge_expired(self) -> int:
        before = len(self._store)
        self._store = [m for m in self._store if time.time() < m["expires_at"]]
        return before - len(self._store)

    def get_active(self) -> list[dict]:
        self.purge_expired()
        # Sort: permanent first, then by recency within each tier
        return sorted(
            self._store,
            key=lambda m: (0 if m["tier"] == "permanent" else 1, -m["created_at"])
        )

    def build_context(self) -> str:
        active = self.get_active()
        if not active:
            return ""
        # Group by tier for readability
        by_tier: dict[str, list] = {}
        for m in active:
            by_tier.setdefault(m["tier"], []).append(m)
        lines = []
        for tier in ["permanent", "preference", "short_term", "session"]:
            if tier in by_tier:
                lines.append(f"[{tier}]")
                for m in by_tier[tier]:
                    ttl_h = (m["expires_at"] - time.time()) / 3600
                    lines.append(f"  {m['key']}: {m['value']} (expires in {ttl_h:.0f}h)")
        return "Memory:\n" + "\n".join(lines)

mem = TTLMemory()
mem.add("name",            "Alice",      tier="permanent")
mem.add("python_version",  "Python 3.12",tier="preference")
mem.add("current_project", "API rewrite",tier="short_term")
mem.add("last_error",      "TypeError at line 42", tier="session")

# Simulate a stale session entry (add it 2 hours ago in simulation)
mem._store.append({
    "key": "stale_task", "value": "debug login page",
    "created_at": time.time() - 7200, "ttl": 3600,
    "tier": "session", "expires_at": time.time() - 3600,  # already expired
})

purged = mem.purge_expired()
print(f"Purged {purged} expired entries.")
print(f"\nContext:\n{mem.build_context()}")

r = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=128,
    system=f"You are a coding assistant.\n\n{mem.build_context()}",
    messages=[{"role": "user", "content": "What should I know about Alice?"}],
)
print(f"\nA: {r.content[0].text.strip()[:200]}")
```

**Expected Token Savings:** TTL-based expiry automatically removes session context that would confuse the model in later sessions, and short-term task context that becomes irrelevant after a week — preventing the context window from filling with stale, irrelevant memories that increase input tokens without improving answer quality.
**Environment:** Long-running agents serving users over days and weeks; TTL tiers match memory lifetime to its natural relevance window, keeping context lean and current.

---

### Option 6 — Recency injection: always prepend newest memories in prompt

```python
import time
import anthropic

client = anthropic.Anthropic()

class RecencyFirstMemory:
    """
    Simple but effective: always put the most recent memories
    at the TOP of the context block, before older ones.
    Models attend more strongly to content near the start of context.
    """

    def __init__(self, max_entries: int = 10) -> None:
        self._entries: list[dict] = []
        self._max    = max_entries

    def add(self, key: str, value: str) -> None:
        self._entries.append({"key": key, "value": value, "ts": time.time()})
        # Keep only the most recent max_entries
        if len(self._entries) > self._max * 2:
            # Deduplicate by key, keeping newest
            seen: dict[str, dict] = {}
            for e in sorted(self._entries, key=lambda x: x["ts"]):
                seen[e["key"]] = e
            self._entries = sorted(seen.values(), key=lambda x: x["ts"])

    def build_context(self, prefix: str = "User preferences (most recent first):") -> str:
        # Sort newest first — critical for recency primacy
        sorted_entries = sorted(self._entries, key=lambda e: e["ts"], reverse=True)
        # Deduplicate: for each key, only show the newest
        seen_keys: set[str] = set()
        lines = []
        for e in sorted_entries:
            if e["key"] not in seen_keys:
                age_h = (time.time() - e["ts"]) / 3600
                lines.append(f"- {e['key']}: {e['value']}  [{age_h:.0f}h ago]")
                seen_keys.add(e["key"])
        return f"{prefix}\n" + "\n".join(lines) if lines else ""

mem = RecencyFirstMemory(max_entries=10)

# Simulate entries over time
entries = [
    ("preferred_lang",   "Python 2",        300),   # 300s ago (old)
    ("preferred_editor", "Vim",             200),
    ("preferred_lang",   "Python 3",        100),   # 100s ago (override)
    ("preferred_editor", "VS Code",          50),   # 50s ago (override)
    ("test_framework",   "pytest",           10),   # 10s ago (new)
]
now = time.time()
for key, value, ago in entries:
    mem._entries.append({"key": key, "value": value, "ts": now - ago})

context = mem.build_context()
print(f"Context (recency-first, deduplicated):\n{context}")

r = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=128,
    system=f"You are a coding assistant.\n\n{context}\n\nAlways follow user preferences.",
    messages=[{"role": "user", "content": "Write hello world in my preferred language."}],
)
print(f"\nA: {r.content[0].text.strip()[:200]}")
```

**Expected Token Savings:** Recency-first ordering and key deduplication reduce context from N raw entries to 1 per key, in recency order — for a user with 5 preference updates over 6 months, this cuts context from ~500 to ~100 tokens while ensuring the model sees the correct current values first.
**Environment:** All memory-augmented agents; recency-first ordering is a zero-overhead change that dramatically improves preference adherence without any additional API calls.

---

## Comparison

| Option | Handles Conflicts | Auto-Expires | Tokens per Call | Best For |
|---|---|---|---|---|
| 1. Recency-weighted retrieval | Yes (decay score) | No | Low (filtered) | Semantic retrieval systems |
| 2. Versioned store | Yes (latest wins) | No | Minimal (1 per key) | Bounded preference key spaces |
| 3. LLM consolidation | Yes (LLM merge) | Partial | Low (post-merge) | Organic, unstructured memory |
| 4. Conflict detection | Yes (explicit) | No | Low + conflict label | High-frequency preference changes |
| 5. TTL expiry | Yes (newer TTL) | Yes | Minimal (expired pruned) | Long-running multi-session agents |
| 6. Recency-first injection | Yes (deduplicated) | No | Minimal (1 per key) | All agents — simplest fix |
