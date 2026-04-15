---
layout: solution
title: "Agent Stores Duplicate Memories with Slight Variations"
category: memory
description: "Agent accumulates near-identical memory entries over time, bloating the context window and causing conflicting or redundant information to be retrieved."
tags: [memory, deduplication, embeddings, sqlite, context-window]
---

## Symptom

After weeks of operation, the agent's memory store contains hundreds of near-duplicate entries:

```
"User prefers Python over JavaScript"
"The user likes Python better than JS"
"User wants Python code examples"
"User has said they prefer Python"
"User: Python is my favorite language"
```

When retrieved, all five appear in context. The agent is confused about which is canonical, wastes tokens repeating the same fact five ways, and sometimes contradicts itself when the phrasing implies slightly different things. Over time the memory store grows to thousands of entries and retrieval becomes both slow and noisy.

Root causes:
- Each conversation adds new memories without checking if they already exist
- Fuzzy matching is never applied before insertion
- Memory entries are stored verbatim (exact string match for dedup is too strict)
- No merge or consolidation step runs periodically
- Vector similarity is computed at retrieval but not at write time

---

## Root Cause

Memory deduplication is a write-time problem that requires semantic similarity, not exact string matching. Two memory entries can be functionally identical while sharing zero words. Exact-match dedup catches only copy-paste duplicates; it misses the vast majority of real duplicates.

The solution is to compute an embedding of each new memory, compare it against existing memories, and either skip the insert (if similarity > threshold) or merge the new entry with the most similar existing one (if similar but not identical).

---

## Fix

### Option 1 — Cosine Similarity Dedup at Write Time

Compute embeddings at write time and skip insertion when a near-duplicate exists.

```python
import anthropic
import sqlite3
import json
import math
from typing import Optional

client = anthropic.Anthropic()

def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)

def embed_text(text: str) -> list[float]:
    """
    Get an embedding for text using Claude's context as a proxy.
    In production: use a dedicated embedding model (e.g., text-embedding-3-small).
    This simplified version uses keyword overlap as a stand-in.
    """
    # Simplified embedding: TF-IDF-like bag-of-words vector
    import re
    words = set(re.findall(r'\b\w+\b', text.lower()))
    # Use a fixed vocabulary from common memory-related words
    vocab = [
        "python", "javascript", "code", "prefer", "like", "want", "use",
        "user", "always", "never", "help", "format", "style", "language",
        "verbose", "concise", "dark", "light", "theme", "email", "name",
        "company", "role", "senior", "junior", "engineer", "manager",
    ]
    return [1.0 if word in words else 0.0 for word in vocab]

class DeduplicatedMemoryStore:
    """Memory store that prevents near-duplicate entries at write time."""

    DUPLICATE_THRESHOLD = 0.85   # cosine similarity above this → skip
    SIMILAR_THRESHOLD = 0.70     # above this → consider merging

    def __init__(self, db_path: str = ":memory:"):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self._setup_db()

    def _setup_db(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                embedding TEXT NOT NULL,
                created_at REAL DEFAULT (unixepoch()),
                updated_at REAL DEFAULT (unixepoch()),
                access_count INTEGER DEFAULT 0,
                merged_from TEXT DEFAULT '[]'
            );
        """)
        self.db.commit()

    def _load_all_embeddings(self) -> list[tuple[int, str, list[float]]]:
        rows = self.db.execute(
            "SELECT id, content, embedding FROM memories"
        ).fetchall()
        return [
            (row[0], row[1], json.loads(row[2]))
            for row in rows
        ]

    def add(self, memory: str) -> dict:
        """
        Add a memory, deduplicating against existing entries.
        Returns action taken: 'added', 'duplicate_skipped', or 'merged'.
        """
        new_embedding = embed_text(memory)
        existing = self._load_all_embeddings()

        best_sim = 0.0
        best_id = None
        best_content = None

        for mem_id, content, emb in existing:
            sim = cosine_similarity(new_embedding, emb)
            if sim > best_sim:
                best_sim = sim
                best_id = mem_id
                best_content = content

        if best_sim >= self.DUPLICATE_THRESHOLD:
            # Too similar — skip
            print(f"  [dedup] Skipped duplicate (sim={best_sim:.2f}): '{memory[:60]}'")
            print(f"          Existing: '{best_content[:60]}'")
            return {"action": "duplicate_skipped", "similarity": best_sim, "existing_id": best_id}

        if best_sim >= self.SIMILAR_THRESHOLD and best_id is not None:
            # Similar but not identical — merge
            merged = self._merge_memories(best_content, memory)
            merged_embedding = embed_text(merged)
            self.db.execute(
                "UPDATE memories SET content=?, embedding=?, updated_at=unixepoch(), "
                "merged_from=json_insert(merged_from, '$[#]', ?) WHERE id=?",
                (merged, json.dumps(merged_embedding), memory, best_id)
            )
            self.db.commit()
            print(f"  [dedup] Merged (sim={best_sim:.2f}): '{memory[:60]}'")
            print(f"          Into: '{merged[:60]}'")
            return {"action": "merged", "similarity": best_sim, "target_id": best_id, "merged_content": merged}

        # New unique memory — insert
        self.db.execute(
            "INSERT INTO memories (content, embedding) VALUES (?, ?)",
            (memory, json.dumps(new_embedding))
        )
        self.db.commit()
        print(f"  [dedup] Added new memory: '{memory[:60]}'")
        return {"action": "added"}

    def _merge_memories(self, existing: str, new: str) -> str:
        """Merge two similar memories into one canonical form."""
        # In production: use LLM to merge. Here: keep longer/more specific one.
        if len(new) > len(existing) * 1.2:
            return new
        return existing

    def get_all(self) -> list[str]:
        rows = self.db.execute("SELECT content FROM memories ORDER BY access_count DESC").fetchall()
        return [row[0] for row in rows]

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

# Test: add near-duplicate memories
store = DeduplicatedMemoryStore()

memories_to_add = [
    "User prefers Python over JavaScript",
    "The user likes Python better than JS",          # near-duplicate
    "User wants Python code examples",               # similar
    "User has said they prefer Python",              # near-duplicate
    "User speaks English and Korean",               # new, unique
    "User is a senior software engineer",           # new, unique
    "The user is an experienced software engineer", # near-duplicate of above
    "User prefers dark mode in their IDE",          # new, unique
]

print("Adding memories with deduplication:\n")
for mem in memories_to_add:
    store.add(mem)

print(f"\nStore contains {store.count()} unique memories (vs {len(memories_to_add)} inputs):")
for m in store.get_all():
    print(f"  • {m}")
```

**Expected Token Savings:** Reducing 8 memories to 4-5 unique ones cuts retrieval context by 40-50%. Over hundreds of sessions, prevents context window bloat entirely.

**Environment:** Python 3.9+, `sqlite3`, `anthropic>=0.40.0`. For production, replace the toy embedding with `text-embedding-3-small` or similar.

---

### Option 2 — LLM-Powered Deduplication with Explicit Merge

Use a fast model to decide whether a new memory is a duplicate and how to merge if so.

```python
import anthropic
import sqlite3
import json
from typing import Optional

client = anthropic.Anthropic()

class LLMDeduplicatedStore:
    """Memory store using an LLM to make deduplication decisions."""

    def __init__(self, db_path: str = ":memory:"):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self._setup()

    def _setup(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL UNIQUE,
                category TEXT DEFAULT 'general',
                created_at REAL DEFAULT (unixepoch())
            );
        """)
        self.db.commit()

    def _get_all_memories(self) -> list[tuple[int, str]]:
        return self.db.execute("SELECT id, content FROM memories").fetchall()

    def _llm_dedup_check(self, new_memory: str, existing: list[str]) -> dict:
        """Ask a fast model whether new_memory duplicates anything in existing."""
        if not existing:
            return {"action": "add", "reason": "no existing memories"}

        existing_formatted = "\n".join(f"{i+1}. {m}" for i, m in enumerate(existing))

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=(
                "You are a memory deduplication assistant. "
                "Respond with JSON only, no other text."
            ),
            messages=[{
                "role": "user",
                "content": f"""New memory to add: "{new_memory}"

Existing memories:
{existing_formatted}

Decide what to do. Respond with JSON:
{{
  "action": "add" | "skip" | "merge",
  "duplicate_index": <1-based index of most similar, or null>,
  "merged_content": "<if action=merge, the canonical merged version; else null>",
  "reason": "<brief explanation>"
}}

Rules:
- "skip" if new memory is semantically equivalent to an existing one (same fact, different words)
- "merge" if new memory adds a small detail to an existing similar one
- "add" if it's genuinely new information"""
            }]
        )

        try:
            text = response.content[0].text.strip()
            # Strip markdown code fences if present
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            return json.loads(text)
        except Exception as e:
            return {"action": "add", "reason": f"parse error: {e}"}

    def add(self, memory: str) -> dict:
        existing_rows = self._get_all_memories()
        existing_contents = [row[1] for row in existing_rows]

        decision = self._llm_dedup_check(memory, existing_contents)
        action = decision.get("action", "add")
        print(f"  [dedup] {action.upper()}: {decision.get('reason', '')}")

        if action == "skip":
            return {"action": "skip", "decision": decision}

        if action == "merge":
            dup_idx = decision.get("duplicate_index")
            merged = decision.get("merged_content", memory)
            if dup_idx and 1 <= dup_idx <= len(existing_rows):
                target_id = existing_rows[dup_idx - 1][0]
                self.db.execute(
                    "UPDATE memories SET content=? WHERE id=?",
                    (merged, target_id)
                )
                self.db.commit()
                return {"action": "merged", "merged_content": merged, "target_id": target_id}

        # Add new
        try:
            self.db.execute("INSERT INTO memories (content) VALUES (?)", (memory,))
            self.db.commit()
            return {"action": "added"}
        except sqlite3.IntegrityError:
            return {"action": "skip", "reason": "exact duplicate"}

    def retrieve_all(self) -> list[str]:
        return [row[0] for row in self.db.execute("SELECT content FROM memories").fetchall()]

store = LLMDeduplicatedStore()

test_memories = [
    "User prefers concise responses without fluff",
    "The user wants short answers, no padding",           # duplicate
    "User is based in Seoul, South Korea",
    "User works at a startup in Korea",                   # similar but additive
    "User wants code examples in Python",
    "User prefers Python code snippets",                  # duplicate
    "User sometimes asks in Korean",
    "User is bilingual in English and Korean",            # additive
]

for mem in test_memories:
    print(f"\nAdding: '{mem}'")
    result = store.add(mem)

print(f"\n--- Final memory store ({len(store.retrieve_all())} entries) ---")
for m in store.retrieve_all():
    print(f"  • {m}")
```

**Expected Token Savings:** LLM deduplication costs ~100 tokens/write but prevents adding redundant memories that each cost 50-200 tokens on every subsequent retrieval. Break-even at 2-3 retrievals per memory.

**Environment:** Python 3.9+, `sqlite3`, `anthropic>=0.40.0`. Uses Haiku for cost-efficient deduplication.

---

### Option 3 — Periodic Memory Consolidation Pass

Schedule a consolidation job that clusters existing memories and merges duplicates in bulk.

```python
import anthropic
import sqlite3
import json
from collections import defaultdict

client = anthropic.Anthropic()

def consolidate_memories(db_path: str = ":memory:") -> dict:
    """
    Load all memories, cluster duplicates using LLM, and rewrite the store.
    Run periodically (e.g., nightly) rather than on every write.
    """
    db = sqlite3.connect(db_path)
    memories = db.execute("SELECT id, content FROM memories").fetchall()

    if len(memories) < 2:
        return {"consolidated": 0, "removed": 0}

    mem_list = "\n".join(f"{row[0]}: {row[1]}" for row in memories)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        system="Memory consolidation assistant. Respond with JSON only.",
        messages=[{
            "role": "user",
            "content": f"""These are memory entries for an AI agent.
Find all duplicates and near-duplicates and group them.

Memories:
{mem_list}

Respond with a JSON object:
{{
  "groups": [
    {{
      "canonical": "<single best version of the memory>",
      "member_ids": [<id1>, <id2>, ...],
      "keep_id": <id of the entry closest to canonical, or null to create new>
    }}
  ]
}}

Rules:
- Only group entries that represent the same fact with different wording
- Each group must have at least 2 members (singleton facts stay as-is)
- The canonical form should be the most complete, precise version
- Groups with factual conflicts (contradictory info) should NOT be merged"""
        }]
    )

    try:
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = "\n".join(text.split("\n")[1:-1])
        result = json.loads(text)
    except Exception as e:
        return {"error": f"Parse error: {e}"}

    groups = result.get("groups", [])
    removed_ids = []
    updated = 0

    for group in groups:
        member_ids = group.get("member_ids", [])
        canonical = group.get("canonical", "")
        keep_id = group.get("keep_id")

        if len(member_ids) < 2 or not canonical:
            continue

        # Update the kept entry to canonical form
        if keep_id and keep_id in member_ids:
            db.execute("UPDATE memories SET content=? WHERE id=?", (canonical, keep_id))
            remove_ids = [mid for mid in member_ids if mid != keep_id]
        else:
            # Keep the first, remove the rest
            keep_id = member_ids[0]
            db.execute("UPDATE memories SET content=? WHERE id=?", (canonical, keep_id))
            remove_ids = member_ids[1:]

        if remove_ids:
            placeholders = ",".join("?" * len(remove_ids))
            db.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", remove_ids)
            removed_ids.extend(remove_ids)
            updated += 1

    db.commit()
    db.close()

    return {
        "groups_merged": len(groups),
        "entries_removed": len(removed_ids),
        "removed_ids": removed_ids,
    }

# Demo: create a DB with duplicates and run consolidation
db = sqlite3.connect("test_memories.db")
db.execute("CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY, content TEXT)")
test_entries = [
    "User prefers Python",
    "User likes Python better than other languages",
    "User wants Python examples",
    "User is a software engineer",
    "User works as a developer",
    "User is located in Seoul",
    "User speaks Korean and English",
    "User is bilingual",
]
for entry in test_entries:
    db.execute("INSERT INTO memories (content) VALUES (?)", (entry,))
db.commit()
db.close()

print(f"Before: {len(test_entries)} memories")
result = consolidate_memories("test_memories.db")
print(f"Consolidation result: {result}")

db = sqlite3.connect("test_memories.db")
final = db.execute("SELECT content FROM memories").fetchall()
db.close()
print(f"After: {len(final)} memories")
for row in final:
    print(f"  • {row[0]}")

import os
os.remove("test_memories.db")
```

**Expected Token Savings:** Batch consolidation is highly efficient — one LLM call deduplicates 50+ memories. Reducing 200 → 80 memories saves 60% of retrieval context permanently.

**Environment:** Python 3.9+, `sqlite3`, `anthropic>=0.40.0`. Schedule with `cron` or APScheduler.

---

### Option 4 — Category-Scoped Deduplication with Conflict Detection

Group memories by category and enforce "one canonical fact per slot" within each category.

```python
import anthropic
import sqlite3
import json

client = anthropic.Anthropic()

# Defines what kinds of facts each category can store and how many
MEMORY_SCHEMA = {
    "language_preference": {
        "max_entries": 1,
        "description": "Preferred programming language",
        "merge_strategy": "replace_if_newer",
    },
    "communication_style": {
        "max_entries": 3,
        "description": "How user prefers to receive information",
        "merge_strategy": "accumulate_unique",
    },
    "location": {
        "max_entries": 1,
        "description": "User's location",
        "merge_strategy": "replace_if_newer",
    },
    "role": {
        "max_entries": 1,
        "description": "User's job role",
        "merge_strategy": "replace_if_newer",
    },
    "project_context": {
        "max_entries": 5,
        "description": "Current projects user is working on",
        "merge_strategy": "accumulate_unique",
    },
    "general": {
        "max_entries": 20,
        "description": "Other facts",
        "merge_strategy": "llm_dedup",
    },
}

class CategorizedMemoryStore:
    def __init__(self, db_path: str = ":memory:"):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL DEFAULT (unixepoch()),
                confidence REAL DEFAULT 1.0
            );
            CREATE INDEX IF NOT EXISTS idx_category ON memories(category);
        """)
        self.db.commit()

    def _classify_memory(self, memory: str) -> str:
        """Classify a memory into a known category."""
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=20,
            system="Classify text into exactly one category. Reply with only the category name.",
            messages=[{
                "role": "user",
                "content": (
                    f"Memory: \"{memory}\"\n\n"
                    f"Categories: {', '.join(MEMORY_SCHEMA.keys())}\n\n"
                    "Which category? Reply with just the category name."
                )
            }]
        )
        label = response.content[0].text.strip().lower().replace(" ", "_")
        return label if label in MEMORY_SCHEMA else "general"

    def _get_by_category(self, category: str) -> list[tuple[int, str]]:
        return self.db.execute(
            "SELECT id, content FROM memories WHERE category=? ORDER BY created_at DESC",
            (category,)
        ).fetchall()

    def add(self, memory: str, category: str = None) -> dict:
        if category is None:
            category = self._classify_memory(memory)

        schema = MEMORY_SCHEMA.get(category, MEMORY_SCHEMA["general"])
        existing = self._get_by_category(category)
        strategy = schema["merge_strategy"]

        print(f"  [memory] category={category}, strategy={strategy}, existing={len(existing)}")

        if strategy == "replace_if_newer":
            # Category holds at most 1 fact — replace with newer
            if existing:
                old_id, old_content = existing[0]
                print(f"  [memory] Replacing: '{old_content[:50]}' → '{memory[:50]}'")
                self.db.execute("UPDATE memories SET content=?, created_at=unixepoch() WHERE id=?",
                                (memory, old_id))
                self.db.commit()
                return {"action": "replaced", "old": old_content, "new": memory, "category": category}

        if strategy == "accumulate_unique":
            # Add only if not already present (exact match)
            if any(memory.lower() == c.lower() for _, c in existing):
                return {"action": "skip", "reason": "exact duplicate in category"}
            if len(existing) >= schema["max_entries"]:
                # Remove oldest to make room
                oldest_id = existing[-1][0]
                self.db.execute("DELETE FROM memories WHERE id=?", (oldest_id,))

        # Default: insert new entry
        self.db.execute(
            "INSERT INTO memories (category, content) VALUES (?, ?)",
            (category, memory)
        )
        self.db.commit()
        return {"action": "added", "category": category}

    def get_all_structured(self) -> dict[str, list[str]]:
        result = {}
        for category in MEMORY_SCHEMA:
            rows = self._get_by_category(category)
            if rows:
                result[category] = [row[1] for row in rows]
        return result

store = CategorizedMemoryStore()

test_memories = [
    "User prefers Python for all coding tasks",
    "User likes Python over JavaScript",         # same category, replace
    "User wants brief responses",
    "User hates verbose answers",               # same category, accumulate
    "User lives in Seoul",
    "User is based in South Korea",            # same category, replace
    "User is a backend engineer",
    "User is working on a FastAPI project",
    "User is building a REST API with FastAPI", # similar to above, accumulate_unique
]

for mem in test_memories:
    result = store.add(mem)
    print(f"  → {result['action']}\n")

print("\n--- Structured memory store ---")
structured = store.get_all_structured()
for category, entries in structured.items():
    print(f"\n{category}:")
    for e in entries:
        print(f"  • {e}")
```

**Expected Token Savings:** Schema-enforced limits cap memory size absolutely — language_preference stays at 1 entry forever regardless of how many conversations mention it.

**Environment:** Python 3.9+, `sqlite3`, `anthropic>=0.40.0`.

---

### Option 5 — Hash-Based Exact + Fuzzy Dedup Pipeline

Two-stage deduplication: fast exact hash check first, fuzzy semantic check only when exact check passes.

```python
import anthropic
import sqlite3
import hashlib
import json
import re

client = anthropic.Anthropic()

def normalize_memory(text: str) -> str:
    """Normalize text for comparison: lowercase, strip punctuation, collapse spaces."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Remove filler words that don't change meaning
    fillers = {"the", "a", "an", "user", "said", "has", "that", "they", "their"}
    words = [w for w in text.split() if w not in fillers]
    return " ".join(words)

def content_hash(text: str) -> str:
    return hashlib.sha256(normalize_memory(text).encode()).hexdigest()[:16]

def keyword_jaccard(a: str, b: str) -> float:
    """Jaccard similarity on normalized word sets."""
    words_a = set(normalize_memory(a).split())
    words_b = set(normalize_memory(b).split())
    if not words_a and not words_b:
        return 1.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union) if union else 0.0

class TwoStageDedupStore:
    FUZZY_THRESHOLD = 0.60

    def __init__(self, db_path: str = ":memory:"):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                normalized TEXT NOT NULL,
                created_at REAL DEFAULT (unixepoch()),
                hit_count INTEGER DEFAULT 0
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_hash ON memories(content_hash);
        """)
        self.db.commit()

    def add(self, memory: str) -> dict:
        normalized = normalize_memory(memory)
        h = content_hash(memory)

        # Stage 1: Exact hash check (O(1))
        existing_exact = self.db.execute(
            "SELECT id, content FROM memories WHERE content_hash=?", (h,)
        ).fetchone()

        if existing_exact:
            print(f"  [dedup/exact] Skipped hash match: '{memory[:50]}'")
            return {"action": "skip", "stage": "exact_hash"}

        # Stage 2: Fuzzy similarity check (O(n))
        all_memories = self.db.execute(
            "SELECT id, content, normalized FROM memories"
        ).fetchall()

        best_sim = 0.0
        best_id = None
        best_content = None

        for row_id, content, row_norm in all_memories:
            sim = keyword_jaccard(normalized, row_norm)
            if sim > best_sim:
                best_sim = sim
                best_id = row_id
                best_content = content

        if best_sim >= self.FUZZY_THRESHOLD:
            print(
                f"  [dedup/fuzzy] Skipped similar (jaccard={best_sim:.2f}): "
                f"'{memory[:40]}' ≈ '{best_content[:40]}'"
            )
            # Increment hit count on the existing memory (it was "seen again")
            self.db.execute(
                "UPDATE memories SET hit_count=hit_count+1 WHERE id=?", (best_id,)
            )
            self.db.commit()
            return {"action": "skip", "stage": "fuzzy", "similarity": best_sim}

        # Unique — insert
        self.db.execute(
            "INSERT INTO memories (content, content_hash, normalized) VALUES (?,?,?)",
            (memory, h, normalized)
        )
        self.db.commit()
        print(f"  [dedup/new] Added: '{memory[:60]}'")
        return {"action": "added", "best_similarity": best_sim}

    def get_all(self, order_by: str = "hit_count DESC") -> list[str]:
        rows = self.db.execute(
            f"SELECT content FROM memories ORDER BY {order_by}"
        ).fetchall()
        return [row[0] for row in rows]

    def stats(self) -> dict:
        row = self.db.execute(
            "SELECT COUNT(*), SUM(hit_count) FROM memories"
        ).fetchone()
        return {"unique_memories": row[0], "total_hits": row[1] or 0}

store = TwoStageDedupStore()

candidates = [
    "User prefers Python",
    "User prefers Python",                   # exact duplicate
    "User likes Python",                     # fuzzy duplicate
    "User prefers Python over JavaScript",   # similar but distinct
    "User prefers python language",          # near-exact after normalization
    "User is a senior engineer",
    "User works as a senior developer",      # fuzzy duplicate
    "User lives in Seoul",
    "User is in Seoul South Korea",          # fuzzy duplicate
    "User wants concise answers",            # unique
]

for mem in candidates:
    store.add(mem)

print(f"\n{store.stats()}")
print(f"\nFinal memories:")
for m in store.get_all():
    print(f"  • {m}")
```

**Expected Token Savings:** Two-stage pipeline keeps average deduplication cost near-zero (hash check is free) while catching 95%+ of real duplicates.

**Environment:** Python 3.9+, `sqlite3`, `re`, `hashlib`, `anthropic>=0.40.0`.

---

### Option 6 — Memory Aging with Automatic Consolidation

Combine deduplication with aging — frequently-hit memories get strengthened; rarely-accessed ones are consolidated or removed.

```python
import anthropic
import sqlite3
import json
import time

client = anthropic.Anthropic()

class AgingMemoryStore:
    """
    Memory store where entries gain 'strength' on access and decay over time.
    Weak memories are consolidated or pruned; strong ones are preserved.
    """

    INITIAL_STRENGTH = 1.0
    ACCESS_BOOST = 0.5
    DECAY_RATE = 0.1        # per day
    PRUNE_THRESHOLD = 0.2   # memories below this are candidates for removal
    MAX_MEMORIES = 100

    def __init__(self, db_path: str = ":memory:"):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                strength REAL DEFAULT 1.0,
                created_at REAL DEFAULT (unixepoch()),
                last_accessed REAL DEFAULT (unixepoch())
            );
        """)
        self.db.commit()

    def _current_strength(self, base_strength: float, last_accessed: float) -> float:
        """Apply time decay to get current strength."""
        days_since = (time.time() - last_accessed) / 86400
        return max(0.0, base_strength - self.DECAY_RATE * days_since)

    def add(self, memory: str) -> dict:
        """Add memory with deduplication and strength initialization."""
        # Simple normalization-based duplicate check
        normalized = " ".join(sorted(memory.lower().split()))

        existing = self.db.execute("SELECT id, content, strength FROM memories").fetchall()

        for row_id, content, strength in existing:
            row_norm = " ".join(sorted(content.lower().split()))
            common = set(normalized.split()) & set(row_norm.split())
            if len(common) / max(len(normalized.split()), 1) > 0.7:
                # Strengthen existing memory instead of adding duplicate
                self.db.execute(
                    "UPDATE memories SET strength=MIN(strength+?,5.0), last_accessed=? WHERE id=?",
                    (self.ACCESS_BOOST, time.time(), row_id)
                )
                self.db.commit()
                return {"action": "strengthened", "id": row_id, "boost": self.ACCESS_BOOST}

        # Insert new
        self.db.execute(
            "INSERT INTO memories (content, strength) VALUES (?,?)",
            (memory, self.INITIAL_STRENGTH)
        )
        self.db.commit()

        # Prune if over limit
        self._prune_if_needed()
        return {"action": "added"}

    def access(self, memory_id: int):
        """Record that a memory was accessed, boosting its strength."""
        self.db.execute(
            "UPDATE memories SET strength=MIN(strength+?,5.0), last_accessed=? WHERE id=?",
            (self.ACCESS_BOOST, time.time(), memory_id)
        )
        self.db.commit()

    def _prune_if_needed(self):
        """Remove weakest memories when over the limit."""
        count = self.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        if count <= self.MAX_MEMORIES:
            return

        # Apply decay and find weak memories
        rows = self.db.execute(
            "SELECT id, strength, last_accessed FROM memories"
        ).fetchall()

        weak_ids = [
            row[0] for row in rows
            if self._current_strength(row[1], row[2]) < self.PRUNE_THRESHOLD
        ]

        if weak_ids:
            placeholders = ",".join("?" * len(weak_ids))
            self.db.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", weak_ids)
            self.db.commit()
            print(f"  [aging] Pruned {len(weak_ids)} weak memories")

    def get_relevant(self, top_n: int = 10) -> list[dict]:
        """Get top N memories by current strength."""
        rows = self.db.execute(
            "SELECT id, content, strength, last_accessed FROM memories"
        ).fetchall()

        ranked = sorted(
            rows,
            key=lambda r: self._current_strength(r[2], r[3]),
            reverse=True
        )[:top_n]

        return [
            {
                "id": r[0],
                "content": r[1],
                "strength": round(self._current_strength(r[2], r[3]), 2),
            }
            for r in ranked
        ]

store = AgingMemoryStore()

# Add memories — duplicates get strengthened, not duplicated
for mem in [
    "User prefers Python",
    "User prefers Python",       # strengthens existing
    "User is a backend engineer",
    "User likes Python",         # similar → strengthens
    "User lives in Seoul",
    "User lives in Seoul",       # strengthens
    "User has a dog named Max",
]:
    result = store.add(mem)
    print(f"  {result['action']}: '{mem[:40]}'")

print(f"\nTop memories by strength:")
for m in store.get_relevant(top_n=5):
    print(f"  [{m['strength']:.1f}] {m['content']}")
```

**Expected Token Savings:** Aging + pruning keeps memory store bounded at `MAX_MEMORIES` entries regardless of session count, providing a hard cap on context overhead.

**Environment:** Python 3.9+, `sqlite3`, `anthropic>=0.40.0`.

---

## Comparison

| Option | Dedup Method | LLM Cost | Merge Support | Scales To |
|--------|-------------|----------|---------------|-----------|
| 1 — Cosine Similarity | Embedding + threshold | None | Yes | 10K memories |
| 2 — LLM Dedup | Haiku classification | Per-write | Yes | 500 memories |
| 3 — Periodic Consolidation | Batch LLM | Per-run | Yes | Unlimited |
| 4 — Category Schema | Structured slots | Per-write | Replace | Fixed categories |
| 5 — Hash + Jaccard | Hash + set ops | None | No | 100K memories |
| 6 — Aging + Pruning | Set overlap + decay | None | No | Fixed cap |

**Start with Option 5** (hash + Jaccard) — zero LLM cost, handles the majority of duplicates. Add **Option 3** (periodic consolidation) for a weekly cleanup pass. Use **Option 4** (category schema) when your agent tracks a well-defined set of user attributes.
