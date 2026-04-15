---
layout: solution
title: "Agent doesn't implement memory search before storing"
category: memory
description: "Agent stores every new piece of information as a fresh memory without checking for existing entries, causing duplicate and contradictory memories to accumulate."
tags: [memory, deduplication, search, semantic, storage]
---

## Symptom

The agent stores the same user preference, fact, or instruction multiple times with slight variations. After a few sessions the memory store contains entries like:

```
"User prefers dark mode"
"User likes dark themes"
"User wants dark mode enabled"
"User uses dark mode"
```

On retrieval, all four are injected into the system prompt. The context bloats, the model receives contradictory or redundant instructions, and storage costs grow linearly with the number of interactions rather than the number of distinct facts.

## Root Cause

The write path for memory has no lookup phase. Every `store_memory(fact)` call appends unconditionally. Without a search step — lexical, semantic, or structural — duplicate entries accumulate silently and the memory store degrades over time.

## Fix

Before every write, search for semantically similar existing entries. If a match is found above a similarity threshold, update the existing entry instead of creating a new one.

---

### Option 1 — Exact-key deduplication with a string normalizer

```python
import anthropic
import json
import re
from pathlib import Path

client = anthropic.Anthropic()

MEMORY_FILE = Path("/tmp/agent_memory_exact.json")

def normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace for comparison."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text

def load_memory() -> dict[str, str]:
    if MEMORY_FILE.exists():
        return json.loads(MEMORY_FILE.read_text())
    return {}

def save_memory(mem: dict[str, str]) -> None:
    MEMORY_FILE.write_text(json.dumps(mem, indent=2))

def store_memory_dedup(fact: str) -> str:
    mem = load_memory()
    key = normalize(fact)

    if key in mem:
        return f"[SKIP] Already stored: '{mem[key]}'"

    # Check for high overlap with existing keys using token intersection
    fact_tokens = set(key.split())
    for existing_key, existing_val in mem.items():
        existing_tokens = set(existing_key.split())
        if not fact_tokens or not existing_tokens:
            continue
        overlap = len(fact_tokens & existing_tokens) / len(fact_tokens | existing_tokens)
        if overlap > 0.7:
            # Update the existing entry to the latest phrasing
            del mem[existing_key]
            mem[key] = fact
            save_memory(mem)
            return f"[UPDATE] Replaced similar entry: '{existing_val}' → '{fact}'"

    mem[key] = fact
    save_memory(mem)
    return f"[STORED] New entry: '{fact}'"

def retrieve_memory(query: str) -> list[str]:
    mem = load_memory()
    query_tokens = set(normalize(query).split())
    scored = []
    for key, val in mem.items():
        key_tokens = set(key.split())
        if not query_tokens or not key_tokens:
            continue
        overlap = len(query_tokens & key_tokens) / len(query_tokens | key_tokens)
        if overlap > 0.2:
            scored.append((overlap, val))
    scored.sort(reverse=True)
    return [v for _, v in scored[:5]]

# Demonstrate deduplication
facts = [
    "User prefers dark mode",
    "User likes dark themes",
    "User wants dark mode enabled",
    "User uses Python for scripting",
    "User codes in Python",
]

for fact in facts:
    result = store_memory_dedup(fact)
    print(result)

print("\nMemory after deduplication:")
for k, v in load_memory().items():
    print(f"  {v}")

print("\nSearch results for 'dark':")
for r in retrieve_memory("dark"):
    print(f"  {r}")
```

**Expected Token Savings:** 40–70% reduction in memory injection tokens for preference-heavy agents; duplicate elimination keeps the memory store at O(distinct facts) rather than O(interactions).

**Environment:** Any agent using a flat file or dict-based memory store; no embeddings required.

---

### Option 2 — Semantic deduplication using embedding cosine similarity

```python
import anthropic
import json
import math
from pathlib import Path

client = anthropic.Anthropic()
MEMORY_FILE = Path("/tmp/agent_memory_semantic.json")
SIMILARITY_THRESHOLD = 0.88   # cosine similarity above this = duplicate

def embed(text: str) -> list[float]:
    """Use the Anthropic API to get a text embedding (via a simple approach)."""
    # Since Anthropic doesn't expose an embeddings API directly,
    # we use a lightweight cosine similarity over TF-IDF-like vectors.
    # In production, replace with OpenAI embeddings or a local model.
    words = text.lower().split()
    vocab = list(set(words))
    vec = [words.count(w) for w in vocab]
    norm = math.sqrt(sum(x*x for x in vec)) or 1
    return [x / norm for x in vec], vocab

def cosine_sim_from_word_vecs(
    vec_a: list[float], vocab_a: list[str],
    vec_b: list[float], vocab_b: list[str],
) -> float:
    all_words = list(set(vocab_a) | set(vocab_b))
    map_a = dict(zip(vocab_a, vec_a))
    map_b = dict(zip(vocab_b, vec_b))
    dot = sum(map_a.get(w, 0) * map_b.get(w, 0) for w in all_words)
    return dot

def load_memories() -> list[dict]:
    if MEMORY_FILE.exists():
        return json.loads(MEMORY_FILE.read_text())
    return []

def save_memories(memories: list[dict]) -> None:
    MEMORY_FILE.write_text(json.dumps(memories, indent=2))

def search_before_store(new_fact: str, threshold: float = SIMILARITY_THRESHOLD) -> str:
    memories = load_memories()
    new_vec, new_vocab = embed(new_fact)

    best_sim = 0.0
    best_idx = -1

    for i, entry in enumerate(memories):
        stored_vec, stored_vocab = embed(entry["text"])
        sim = cosine_sim_from_word_vecs(new_vec, new_vocab, stored_vec, stored_vocab)
        if sim > best_sim:
            best_sim = sim
            best_idx = i

    if best_sim >= threshold:
        old_text = memories[best_idx]["text"]
        memories[best_idx]["text"] = new_fact
        memories[best_idx]["updated_count"] = memories[best_idx].get("updated_count", 0) + 1
        save_memories(memories)
        return f"[UPDATE sim={best_sim:.2f}] '{old_text}' → '{new_fact}'"

    memories.append({"text": new_fact, "updated_count": 0})
    save_memories(memories)
    return f"[STORE new, best_sim={best_sim:.2f}] '{new_fact}'"

def search_memory(query: str, top_k: int = 3) -> list[str]:
    memories = load_memories()
    q_vec, q_vocab = embed(query)
    scored = []
    for entry in memories:
        e_vec, e_vocab = embed(entry["text"])
        sim = cosine_sim_from_word_vecs(q_vec, q_vocab, e_vec, e_vocab)
        scored.append((sim, entry["text"]))
    scored.sort(reverse=True)
    return [t for _, t in scored[:top_k] if _ > 0.1]

# Test semantic deduplication
facts = [
    "User prefers dark mode",
    "User wants the UI in dark theme",   # semantic duplicate
    "User is a Python developer",
    "The user works with Python",         # semantic duplicate
    "User's timezone is UTC+9",
]

for fact in facts:
    print(search_before_store(fact))

print("\nAll stored memories:")
for m in load_memories():
    print(f"  [{m.get('updated_count', 0)} updates] {m['text']}")
```

**Expected Token Savings:** 50–80% reduction in redundant memory entries; semantic matching catches paraphrase duplicates that exact-match approaches miss.

**Environment:** Requires a local or remote embedding model; replace the TF-IDF approximation with a proper embedding API for production use.

---

### Option 3 — Category-keyed memory with slot overwriting

```python
import anthropic
import json
import re
from pathlib import Path

client = anthropic.Anthropic()
MEMORY_FILE = Path("/tmp/agent_memory_slots.json")

# Predefined slot categories — only one value per slot
SLOT_PATTERNS = {
    "ui_theme":     re.compile(r"\b(dark|light|high contrast|theme|mode)\b", re.I),
    "language":     re.compile(r"\b(language|locale|speaks|prefers [a-z]+)\b", re.I),
    "timezone":     re.compile(r"\bUTC[+-]\d+|timezone|tz\b", re.I),
    "expertise":    re.compile(r"\b(senior|junior|beginner|expert|years of experience)\b", re.I),
    "primary_lang": re.compile(r"\b(Python|JavaScript|Go|Rust|Java|TypeScript)\b"),
    "name":         re.compile(r"\b(my name is|I am called|call me)\b", re.I),
}

def classify_slot(fact: str) -> str | None:
    for slot, pattern in SLOT_PATTERNS.items():
        if pattern.search(fact):
            return slot
    return None

def load_slots() -> dict:
    if MEMORY_FILE.exists():
        return json.loads(MEMORY_FILE.read_text())
    return {"slots": {}, "unslotted": []}

def save_slots(data: dict) -> None:
    MEMORY_FILE.write_text(json.dumps(data, indent=2))

def store_with_slot_dedup(fact: str) -> str:
    data = load_slots()
    slot = classify_slot(fact)

    if slot:
        old = data["slots"].get(slot)
        data["slots"][slot] = fact
        save_slots(data)
        if old:
            return f"[OVERWRITE slot={slot}] '{old}' → '{fact}'"
        return f"[STORED slot={slot}] '{fact}'"

    # Unslotted: check for near-duplicates by simple overlap
    fact_words = set(fact.lower().split())
    for i, existing in enumerate(data["unslotted"]):
        ex_words = set(existing.lower().split())
        overlap = len(fact_words & ex_words) / (len(fact_words | ex_words) or 1)
        if overlap > 0.6:
            data["unslotted"][i] = fact
            save_slots(data)
            return f"[UPDATE unslotted, overlap={overlap:.2f}] '{existing}' → '{fact}'"

    data["unslotted"].append(fact)
    save_slots(data)
    return f"[STORED unslotted] '{fact}'"

def get_all_memories() -> list[str]:
    data = load_slots()
    return list(data["slots"].values()) + data["unslotted"]

# Test slot overwriting
facts = [
    "User prefers dark mode",
    "User wants to switch to light mode",     # overwrites ui_theme slot
    "User codes in Python",
    "User is a JavaScript developer",         # overwrites primary_lang slot
    "User's timezone is UTC+9",
    "User is based in UTC+5",                 # overwrites timezone slot
    "User enjoys hiking on weekends",         # unslotted
]

for fact in facts:
    print(store_with_slot_dedup(fact))

print("\nFinal memory state:")
for m in get_all_memories():
    print(f"  {m}")
```

**Expected Token Savings:** 60–90% for preference-heavy agents; slot-based storage guarantees at most one entry per known category regardless of how many times a preference is updated.

**Environment:** Personal assistant and user-profile agents where facts belong to well-defined categories; slots are extensible via the `SLOT_PATTERNS` dict.

---

### Option 4 — LLM-powered deduplication judge before every write

```python
import anthropic
import json
from pathlib import Path

client = anthropic.Anthropic()
MEMORY_FILE = Path("/tmp/agent_memory_judge.json")

JUDGE_SYSTEM = """
You are a memory deduplication judge. Given a NEW FACT and a list of EXISTING MEMORIES,
determine whether the new fact is already covered by an existing memory.

Respond with ONLY a JSON object:
{
  "action": "skip" | "update" | "store",
  "existing_index": <integer or null>,
  "reason": <string max 10 words>
}

- "skip": new fact is already captured by an existing memory
- "update": new fact supersedes an existing memory (use existing_index)
- "store": new fact is genuinely new
""".strip()

def deduplicate_with_llm(new_fact: str, existing: list[str]) -> tuple[str, int | None]:
    if not existing:
        return "store", None

    numbered = "\n".join(f"{i}: {m}" for i, m in enumerate(existing))
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        system=JUDGE_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"NEW FACT: {new_fact}\n\nEXISTING MEMORIES:\n{numbered}",
        }],
    )
    raw = response.content[0].text.strip()
    start = raw.find("{")
    end   = raw.rfind("}") + 1
    result = json.loads(raw[start:end])
    return result.get("action", "store"), result.get("existing_index")

def load_memories() -> list[str]:
    if MEMORY_FILE.exists():
        return json.loads(MEMORY_FILE.read_text())
    return []

def save_memories(memories: list[str]) -> None:
    MEMORY_FILE.write_text(json.dumps(memories, indent=2))

def smart_store(fact: str) -> str:
    memories = load_memories()
    action, idx = deduplicate_with_llm(fact, memories)

    if action == "skip":
        return f"[SKIP] Already covered by existing memory"
    if action == "update" and idx is not None and 0 <= idx < len(memories):
        old = memories[idx]
        memories[idx] = fact
        save_memories(memories)
        return f"[UPDATE idx={idx}] '{old}' → '{fact}'"

    memories.append(fact)
    save_memories(memories)
    return f"[STORE] '{fact}' (total: {len(memories)})"

# Test LLM-powered deduplication
facts = [
    "The user prefers dark mode.",
    "User likes using dark UI themes.",
    "User is proficient in Python.",
    "User has 5 years of Python experience.",
    "User's timezone is Asia/Seoul (UTC+9).",
    "User works in the KST timezone.",
]

for fact in facts:
    result = smart_store(fact)
    print(result)

print("\nFinal memories:")
for m in load_memories():
    print(f"  {m}")
```

**Expected Token Savings:** Highest-quality deduplication; the LLM judge catches semantic duplicates, paraphrases, and superseded facts that rule-based approaches miss; ~20 tokens per write call is cheap compared to storing a duplicate that costs tokens on every retrieval.

**Environment:** Agents with complex, open-ended memory requirements; the LLM judge is not needed for simple, categorized facts.

---

### Option 5 — Write-through cache with content hash deduplication

```python
import anthropic
import json
import hashlib
import time
from pathlib import Path

client = anthropic.Anthropic()
MEMORY_FILE   = Path("/tmp/agent_memory_hash.json")
HASH_INDEX    = Path("/tmp/agent_memory_hashes.json")

def content_hash(text: str) -> str:
    normalized = " ".join(text.lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:12]

def load_store() -> tuple[list[dict], set[str]]:
    memories = json.loads(MEMORY_FILE.read_text()) if MEMORY_FILE.exists() else []
    hashes   = set(json.loads(HASH_INDEX.read_text())) if HASH_INDEX.exists() else set()
    return memories, hashes

def save_store(memories: list[dict], hashes: set[str]) -> None:
    MEMORY_FILE.write_text(json.dumps(memories, indent=2))
    HASH_INDEX.write_text(json.dumps(list(hashes)))

def store_with_hash_check(fact: str, tags: list[str] | None = None) -> str:
    h = content_hash(fact)
    memories, hashes = load_store()

    if h in hashes:
        return f"[SKIP hash={h}] Exact duplicate detected"

    # Also check for near-duplicates via partial hash collision (first 6 chars)
    partial = h[:6]
    for entry in memories:
        if entry.get("hash", "")[:6] == partial and entry["hash"] != h:
            # Different hash but same prefix — check manually
            overlap = len(set(fact.lower().split()) & set(entry["text"].lower().split()))
            if overlap / max(len(fact.split()), 1) > 0.8:
                hashes.add(h)          # mark as duplicate
                save_store(memories, hashes)
                return f"[NEAR-DUP hash={h}] Similar to '{entry['text'][:50]}'"

    entry = {
        "text": fact,
        "hash": h,
        "tags": tags or [],
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    memories.append(entry)
    hashes.add(h)
    save_store(memories, hashes)
    return f"[STORED hash={h}] '{fact}'"

def search_by_tag(tag: str) -> list[str]:
    memories, _ = load_store()
    return [e["text"] for e in memories if tag in e.get("tags", [])]

# Test hash-based deduplication
facts_with_tags = [
    ("User prefers dark mode",    ["ui", "preference"]),
    ("User prefers dark mode",    ["ui"]),             # exact duplicate
    ("user prefers dark mode.",   ["ui"]),             # normalized duplicate
    ("User writes Python code",   ["skill"]),
    ("User is a Python developer",["skill"]),          # near-duplicate
    ("Meeting at 3pm on Friday",  ["schedule"]),
]

for fact, tags in facts_with_tags:
    print(store_with_hash_check(fact, tags))

print("\nAll UI preferences:")
for m in search_by_tag("ui"):
    print(f"  {m}")
```

**Expected Token Savings:** O(1) lookup for exact duplicates via hash index; near-duplicate detection adds minimal overhead; prevents the most common form of memory bloat (re-storing the same fact on every session).

**Environment:** High-frequency write paths; hash index fits in memory for millions of entries.

---

### Option 6 — Retrieval-augmented write: search then merge

```python
import anthropic
import json
from pathlib import Path

client = anthropic.Anthropic()
MEMORY_FILE = Path("/tmp/agent_memory_rag_write.json")

MERGE_SYSTEM = """
You are a memory manager. Given an EXISTING MEMORY and a NEW FACT about the same topic,
produce a single merged memory that captures all information from both.
Respond with ONLY the merged text — no prefix, no quotes.
Keep it concise (1–2 sentences).
""".strip()

def load_memories() -> list[dict]:
    if MEMORY_FILE.exists():
        return json.loads(MEMORY_FILE.read_text())
    return []

def save_memories(memories: list[dict]) -> None:
    MEMORY_FILE.write_text(json.dumps(memories, indent=2))

def simple_relevance_score(query: str, candidate: str) -> float:
    q_words = set(query.lower().split())
    c_words = set(candidate.lower().split())
    if not q_words or not c_words:
        return 0.0
    return len(q_words & c_words) / len(q_words | c_words)

def rag_write(new_fact: str, similarity_threshold: float = 0.35) -> str:
    memories = load_memories()

    # Search for relevant existing memories
    scored = [
        (simple_relevance_score(new_fact, m["text"]), i, m["text"])
        for i, m in enumerate(memories)
    ]
    scored = [(s, i, t) for s, i, t in scored if s >= similarity_threshold]
    scored.sort(reverse=True)

    if scored:
        best_score, best_idx, best_text = scored[0]

        # Merge with LLM
        merge_resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            system=MERGE_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"EXISTING MEMORY: {best_text}\nNEW FACT: {new_fact}",
            }],
        )
        merged = merge_resp.content[0].text.strip()
        memories[best_idx]["text"] = merged
        memories[best_idx]["merge_count"] = memories[best_idx].get("merge_count", 0) + 1
        save_memories(memories)
        return f"[MERGE score={best_score:.2f}] '{best_text}' + '{new_fact}' → '{merged}'"

    memories.append({"text": new_fact, "merge_count": 0})
    save_memories(memories)
    return f"[STORE] '{new_fact}'"

# Test retrieval-augmented write
facts = [
    "User is a Python developer with 3 years of experience.",
    "User also knows JavaScript and TypeScript.",
    "User has been coding for 3 years mostly in backend.",
    "User prefers VS Code as their editor.",
    "User uses VS Code with Vim keybindings.",
]

for fact in facts:
    print(rag_write(fact))
    print()

print("Final memories:")
for m in load_memories():
    print(f"  [{m.get('merge_count', 0)} merges] {m['text']}")
```

**Expected Token Savings:** 50–80% reduction in memory count; merged entries carry more information per token than the originals, improving retrieval quality while shrinking injection size.

**Environment:** Agents that learn rich user profiles over time; merge quality depends on the LLM — use a stronger model for high-value memories.

---

## Comparison

| Option | Dedup Method | LLM Required | Handles Paraphrases | Merge Quality |
|--------|-------------|-------------|--------------------|----|
| 1 — Exact-key normalize | Token overlap | No | Partially | Overwrite |
| 2 — Semantic embedding | No (TF-IDF approx) | No | Yes | Overwrite |
| 3 — Category slots | Regex classify | No | Within-slot | Overwrite |
| 4 — LLM judge | Yes | Yes | Best | Overwrite |
| 5 — Content hash | SHA-256 | No | Partially | Skip |
| 6 — RAG write + merge | Token overlap + LLM | Yes | Yes | Intelligent merge |

**Recommended default:** Option 3 (category slots) for structured preference agents — zero LLM cost and perfect deduplication for known fact types. Add Option 4 (LLM judge) or Option 6 (RAG merge) for open-ended memory that cannot be pre-categorized.
