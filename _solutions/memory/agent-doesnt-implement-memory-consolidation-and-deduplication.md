---
layout: solution
title: "Agent Doesn't Implement Memory Consolidation and Deduplication"
category: memory
description: "Agents that write memories without consolidation accumulate redundant, contradictory, and near-duplicate entries — memory bloat slows retrieval, wastes tokens, and degrades answer quality over time."
tags: [memory, consolidation, deduplication, cleanup, embedding, similarity, maintenance]
---

# Agent Doesn't Implement Memory Consolidation and Deduplication

## Problem

Agents that accumulate memories session over session without pruning end up with hundreds of near-duplicate entries: "User prefers Python" stored 40 times from 40 different conversations. Retrieval becomes noisy (identical facts compete with each other), context injection bloats (10 variants of the same fact consume 10× the tokens), and contradictions accumulate silently ("User is a beginner" + "User is an expert"). Memory consolidation merges near-duplicates, resolves contradictions, and compresses verbose memories — keeping the store lean and accurate.

## Solutions

### Option 1: Exact and Near-Duplicate Detection with Hashing

Hash-based exact duplicate removal plus sliding-window similarity for near-duplicates — no ML required.

```python
import hashlib
import sqlite3
import json
import time
from difflib import SequenceMatcher
from datetime import datetime

class ConsolidatingMemoryStore:
    def __init__(self, db_path: str = ":memory:", similarity_threshold: float = 0.85):
        self.conn = sqlite3.connect(db_path)
        self.threshold = similarity_threshold
        self._init_db()

    def _init_db(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                category TEXT,
                frequency INTEGER DEFAULT 1,
                first_seen TEXT,
                last_seen TEXT,
                UNIQUE(content_hash)
            )
        """)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_hash ON memories(content_hash)")
        self.conn.commit()

    def _hash(self, text: str) -> str:
        return hashlib.sha256(text.strip().lower().encode()).hexdigest()[:16]

    def _similarity(self, a: str, b: str) -> float:
        return SequenceMatcher(None, a.lower(), b.lower()).ratio()

    def _find_near_duplicate(self, content: str) -> int | None:
        """Return ID of existing memory that's highly similar to content."""
        rows = self.conn.execute(
            "SELECT id, content FROM memories"
        ).fetchall()
        for row_id, existing in rows:
            if self._similarity(content, existing) >= self.threshold:
                return row_id
        return None

    def write(self, content: str, category: str = "general") -> dict:
        content = content.strip()
        h = self._hash(content)
        now = datetime.now().isoformat()

        # Exact duplicate
        existing = self.conn.execute(
            "SELECT id, frequency FROM memories WHERE content_hash = ?", (h,)
        ).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE memories SET frequency = frequency + 1, last_seen = ? WHERE id = ?",
                (now, existing[0])
            )
            self.conn.commit()
            return {"action": "incremented", "id": existing[0], "content": content}

        # Near-duplicate
        dup_id = self._find_near_duplicate(content)
        if dup_id:
            self.conn.execute(
                "UPDATE memories SET frequency = frequency + 1, last_seen = ? WHERE id = ?",
                (now, dup_id)
            )
            self.conn.commit()
            return {"action": "merged_near_dup", "id": dup_id, "content": content}

        # New memory
        cur = self.conn.execute(
            "INSERT INTO memories (content, content_hash, category, first_seen, last_seen) VALUES (?, ?, ?, ?, ?)",
            (content, h, category, now, now)
        )
        self.conn.commit()
        return {"action": "stored_new", "id": cur.lastrowid, "content": content}

    def get_all(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, content, category, frequency, last_seen FROM memories ORDER BY frequency DESC"
        ).fetchall()
        return [
            {"id": r[0], "content": r[1], "category": r[2], "frequency": r[3], "last_seen": r[4]}
            for r in rows
        ]

    def stats(self) -> dict:
        total = self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        high_freq = self.conn.execute(
            "SELECT COUNT(*) FROM memories WHERE frequency > 1"
        ).fetchone()[0]
        return {"total_unique": total, "consolidated_entries": high_freq}

store = ConsolidatingMemoryStore(similarity_threshold=0.85)

# Simulate many sessions writing similar memories
duplicates = [
    ("User prefers Python", "preference"),
    ("User prefers python programming", "preference"),  # Near-dup
    ("User PREFERS python", "preference"),              # Exact dup (after normalize)
    ("User likes Python", "preference"),               # Near-dup
    ("User is a senior engineer", "fact"),
    ("User is a senior software engineer", "fact"),    # Near-dup
    ("The user is a senior engineer", "fact"),         # Near-dup
]

for content, category in duplicates:
    result = store.write(content, category)
    print(f"[{result['action']:20}] {content}")

print(f"\nMemory stats: {store.stats()}")
print("\nConsolidated memories:")
for m in store.get_all():
    print(f"  [freq={m['frequency']}] {m['content']}")
# Expected Token Savings: 60-80% reduction in injected memory tokens after consolidation
# Environment: Long-running personal assistants, CRM agents, session-persistent bots
```

### Option 2: LLM-Driven Contradiction Detection and Resolution

Use the model to identify contradictory memories and resolve them into a single authoritative fact.

```python
import anthropic
import sqlite3
import json
from datetime import datetime

client = anthropic.Anthropic()

class ContradictionResolver:
    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                category TEXT,
                confidence REAL DEFAULT 1.0,
                superseded INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)
        self.conn.commit()

    def add_memory(self, content: str, category: str = "general", confidence: float = 1.0) -> int:
        cur = self.conn.execute(
            "INSERT INTO memories (content, category, confidence, created_at) VALUES (?, ?, ?, ?)",
            (content, category, confidence, datetime.now().isoformat())
        )
        self.conn.commit()
        return cur.lastrowid

    def get_active_by_category(self, category: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, content, confidence FROM memories WHERE category=? AND superseded=0",
            (category,)
        ).fetchall()
        return [{"id": r[0], "content": r[1], "confidence": r[2]} for r in rows]

    def supersede(self, memory_id: int):
        self.conn.execute("UPDATE memories SET superseded=1 WHERE id=?", (memory_id,))
        self.conn.commit()

    def detect_and_resolve_contradictions(self, category: str) -> dict:
        memories = self.get_active_by_category(category)
        if len(memories) < 2:
            return {"contradictions_found": 0, "resolved": []}

        memories_text = "\n".join(
            f"{i+1}. [ID:{m['id']}] {m['content']}"
            for i, m in enumerate(memories)
        )

        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": f"""Analyze these memories for contradictions:

{memories_text}

Find memories that directly contradict each other. For each contradicting pair:
1. Identify which is likely more accurate (more specific, more recent)
2. Write a consolidated version

Return JSON:
{{
  "contradictions": [
    {{
      "ids_to_remove": [list of IDs to supersede],
      "keep_id": ID to keep or null if new,
      "consolidated": "the single authoritative memory text"
    }}
  ],
  "no_contradictions": true/false
}}"""}]
        )

        try:
            result = json.loads(resp.content[0].text)
        except json.JSONDecodeError:
            return {"contradictions_found": 0, "resolved": [], "error": "parse_failed"}

        resolved = []
        for contradiction in result.get("contradictions", []):
            ids_to_remove = contradiction.get("ids_to_remove", [])
            consolidated = contradiction.get("consolidated", "")

            for rid in ids_to_remove:
                self.supersede(rid)

            if consolidated:
                new_id = self.add_memory(consolidated, category, confidence=0.9)
                resolved.append({
                    "removed_ids": ids_to_remove,
                    "new_memory": consolidated,
                    "new_id": new_id,
                })

        return {
            "contradictions_found": len(resolved),
            "resolved": resolved,
            "no_contradictions": result.get("no_contradictions", True),
        }

resolver = ContradictionResolver()

# Add contradictory memories
resolver.add_memory("User is a Python beginner", "user_level")
resolver.add_memory("User has been coding Python for 5 years", "user_level")
resolver.add_memory("User is a senior Python developer", "user_level")
resolver.add_memory("User prefers dark mode in their editor", "preferences")
resolver.add_memory("User uses light mode theme", "preferences")

for category in ["user_level", "preferences"]:
    result = resolver.detect_and_resolve_contradictions(category)
    print(f"\nCategory '{category}': {result['contradictions_found']} contradiction(s) resolved")
    for r in result.get("resolved", []):
        print(f"  Removed IDs {r['removed_ids']} → '{r['new_memory']}'")

print("\nActive memories after resolution:")
for cat in ["user_level", "preferences"]:
    memories = resolver.get_active_by_category(cat)
    for m in memories:
        print(f"  [{cat}] {m['content']}")
# Expected Token Savings: Contradiction elimination removes ~50% of conflicting memories
# Environment: Long-term assistants, user profile systems, fact-tracking agents
```

### Option 3: Periodic Batch Consolidation with Clustering

Cluster all memories by semantic similarity and merge each cluster into a single canonical memory.

```python
import anthropic
import json
import time
from collections import defaultdict

client = anthropic.Anthropic()

# Simulated memory store (replace with real DB)
MEMORIES = [
    {"id": 1, "content": "User prefers Python over Java", "category": "tech"},
    {"id": 2, "content": "User likes Python better than Java", "category": "tech"},
    {"id": 3, "content": "User is learning Rust", "category": "tech"},
    {"id": 4, "content": "User has started learning Rust recently", "category": "tech"},
    {"id": 5, "content": "User's deadline is April 30", "category": "project"},
    {"id": 6, "content": "Project deadline is end of April", "category": "project"},
    {"id": 7, "content": "User works in Berlin", "category": "personal"},
    {"id": 8, "content": "User is based in Berlin, Germany", "category": "personal"},
    {"id": 9, "content": "User is a backend engineer", "category": "role"},
]

def cluster_and_consolidate(memories: list[dict]) -> list[dict]:
    """Use LLM to cluster and consolidate a list of memories."""
    if len(memories) <= 1:
        return memories

    memories_text = "\n".join(
        f"[ID:{m['id']}] {m['content']}"
        for m in memories
    )

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        messages=[{"role": "user", "content": f"""Group these memories by topic and consolidate each group into a single canonical memory. Remove redundancy while preserving all distinct information.

Memories:
{memories_text}

Return JSON:
{{
  "consolidated_memories": [
    {{
      "original_ids": [list of IDs merged],
      "canonical": "single consolidated memory text",
      "confidence": 0.0-1.0
    }}
  ]
}}"""}]
    )

    try:
        result = json.loads(resp.content[0].text)
        return result.get("consolidated_memories", [])
    except json.JSONDecodeError:
        return [{"original_ids": [m["id"] for m in memories], "canonical": m["content"], "confidence": 0.5} for m in memories]

def run_consolidation(memories: list[dict]) -> dict:
    # Group by category first
    by_category = defaultdict(list)
    for m in memories:
        by_category[m.get("category", "general")].append(m)

    all_consolidated = []
    for category, group in by_category.items():
        consolidated = cluster_and_consolidate(group)
        for item in consolidated:
            item["category"] = category
        all_consolidated.extend(consolidated)

    original_count = len(memories)
    consolidated_count = len(all_consolidated)
    reduction = (1 - consolidated_count / max(original_count, 1)) * 100

    return {
        "original_count": original_count,
        "consolidated_count": consolidated_count,
        "reduction_pct": round(reduction, 1),
        "memories": all_consolidated,
    }

result = run_consolidation(MEMORIES)
print(f"Consolidated: {result['original_count']} → {result['consolidated_count']} memories ({result['reduction_pct']}% reduction)\n")
for m in result["memories"]:
    print(f"[{m['category']:10}] (merged {m['original_ids']}) {m['canonical']}")
# Expected Token Savings: 30-60% reduction in memory store size; fewer tokens injected per request
# Environment: Nightly memory maintenance jobs, weekly consolidation cron tasks
```

### Option 4: Frequency-Weighted Memory Pruning

Memories that are frequently confirmed across sessions are high value; memories mentioned once and never seen again are low value. Prune low-frequency, old memories automatically.

```python
import sqlite3
import time
from datetime import datetime, timedelta
from dataclasses import dataclass

@dataclass
class MemoryScore:
    memory_id: int
    content: str
    frequency: int
    age_days: float
    score: float
    action: str

class FrequencyWeightedPruner:
    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                frequency INTEGER DEFAULT 1,
                importance REAL DEFAULT 0.5,
                created_at REAL,
                last_confirmed REAL
            )
        """)
        self.conn.commit()

    def add(self, content: str, importance: float = 0.5) -> int:
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO memories (content, frequency, importance, created_at, last_confirmed) VALUES (?, 1, ?, ?, ?)",
            (content, importance, now, now)
        )
        self.conn.commit()
        return cur.lastrowid

    def confirm(self, memory_id: int):
        """Call when a memory is referenced in a session — boosts its score."""
        self.conn.execute(
            "UPDATE memories SET frequency = frequency + 1, last_confirmed = ? WHERE id = ?",
            (time.time(), memory_id)
        )
        self.conn.commit()

    def compute_score(self, frequency: int, importance: float, age_days: float) -> float:
        """Score = importance × frequency × recency_factor."""
        recency = max(0.1, 1.0 / (1.0 + age_days / 30.0))  # Decay over 30-day half-life
        return importance * min(frequency, 10) * recency

    def prune(self, min_score: float = 0.5, max_age_days: float = 90) -> dict:
        now = time.time()
        rows = self.conn.execute(
            "SELECT id, content, frequency, importance, created_at, last_confirmed FROM memories"
        ).fetchall()

        to_prune = []
        to_keep = []

        for row in rows:
            mid, content, freq, importance, created_at, last_confirmed = row
            age_days = (now - last_confirmed) / 86400.0
            score = self.compute_score(freq, importance, age_days)

            ms = MemoryScore(
                memory_id=mid, content=content, frequency=freq,
                age_days=round(age_days, 1), score=round(score, 3),
                action="keep" if score >= min_score and age_days < max_age_days else "prune"
            )

            if ms.action == "prune":
                to_prune.append(ms)
            else:
                to_keep.append(ms)

        # Execute pruning
        if to_prune:
            self.conn.executemany(
                "DELETE FROM memories WHERE id = ?",
                [(m.memory_id,) for m in to_prune]
            )
            self.conn.commit()

        return {
            "pruned": len(to_prune),
            "kept": len(to_keep),
            "pruned_memories": [{"id": m.memory_id, "content": m.content[:60], "score": m.score} for m in to_prune],
        }

    def get_top(self, n: int = 10) -> list[dict]:
        now = time.time()
        rows = self.conn.execute(
            "SELECT id, content, frequency, importance, last_confirmed FROM memories"
        ).fetchall()
        scored = []
        for row in rows:
            mid, content, freq, importance, last_confirmed = row
            age_days = (now - last_confirmed) / 86400.0
            score = self.compute_score(freq, importance, age_days)
            scored.append({"id": mid, "content": content, "score": round(score, 3), "frequency": freq})
        return sorted(scored, key=lambda x: x["score"], reverse=True)[:n]

pruner = FrequencyWeightedPruner()

# Simulate memories with different confirmation patterns
m1 = pruner.add("User prefers Python", importance=0.9)
m2 = pruner.add("User tried Rust once", importance=0.3)
m3 = pruner.add("User's favorite color is blue", importance=0.1)
m4 = pruner.add("User is a backend engineer", importance=0.8)
m5 = pruner.add("User mentioned liking coffee", importance=0.1)

# Simulate confirmations over time
for _ in range(5):
    pruner.confirm(m1)  # Frequently confirmed
    pruner.confirm(m4)  # Frequently confirmed
pruner.confirm(m2)  # Rarely confirmed

result = pruner.prune(min_score=0.3)
print(f"Pruning: kept {result['kept']}, pruned {result['pruned']}")
print("\nPruned memories:")
for m in result["pruned_memories"]:
    print(f"  [score={m['score']}] {m['content']}")
print("\nTop retained memories:")
for m in pruner.get_top(5):
    print(f"  [score={m['score']} freq={m['frequency']}] {m['content']}")
# Expected Token Savings: 20-50% of low-value memories pruned = proportionally fewer tokens injected
# Environment: Personal assistants with months of history; scheduled nightly pruning jobs
```

### Option 5: Embedding-Based Deduplication Pipeline

Use semantic embeddings to find memories that express the same idea in different words and merge them.

```python
import anthropic
import numpy as np
import json
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class Memory:
    memory_id: str
    content: str
    embedding: list[float] = field(default_factory=list)
    merged_into: str | None = None

def stub_embed(text: str) -> list[float]:
    """Stub embedder — replace with voyage-3 or text-embedding-3-small in production."""
    import hashlib
    h = hashlib.md5(text.lower().encode()).digest()
    vec = [(b / 255.0) * 2 - 1 for b in h]
    return (vec * 2)[:32]  # 32-dim pseudo-embedding

def cosine_sim(a: list[float], b: list[float]) -> float:
    va, vb = np.array(a), np.array(b)
    denom = np.linalg.norm(va) * np.linalg.norm(vb)
    return float(np.dot(va, vb) / denom) if denom > 0 else 0.0

def find_clusters(memories: list[Memory], threshold: float = 0.90) -> list[list[Memory]]:
    """Group memories with cosine similarity > threshold into clusters."""
    used = set()
    clusters = []

    for i, m_i in enumerate(memories):
        if i in used:
            continue
        cluster = [m_i]
        used.add(i)
        for j, m_j in enumerate(memories):
            if j in used or j == i:
                continue
            sim = cosine_sim(m_i.embedding, m_j.embedding)
            if sim >= threshold:
                cluster.append(m_j)
                used.add(j)
        clusters.append(cluster)

    return clusters

def merge_cluster(cluster: list[Memory]) -> str:
    """Use LLM to merge a cluster of similar memories into one."""
    if len(cluster) == 1:
        return cluster[0].content

    memories_text = "\n".join(f"- {m.content}" for m in cluster)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content": f"Merge these memories into one concise sentence preserving all distinct information:\n{memories_text}\n\nMerged memory (one sentence only):"}]
    )
    return resp.content[0].text.strip()

def deduplicate_memories(raw_memories: list[str], threshold: float = 0.88) -> dict:
    memories = [
        Memory(memory_id=f"m_{i:03d}", content=content)
        for i, content in enumerate(raw_memories)
    ]

    # Embed all memories
    for m in memories:
        m.embedding = stub_embed(m.content)

    # Cluster similar ones
    clusters = find_clusters(memories, threshold)

    # Merge each cluster
    deduplicated = []
    stats = {"original": len(memories), "clusters": len(clusters)}

    for cluster in clusters:
        merged_content = merge_cluster(cluster)
        deduplicated.append({
            "content": merged_content,
            "merged_from": [m.memory_id for m in cluster],
            "count_merged": len(cluster),
        })

    stats["deduplicated"] = len(deduplicated)
    stats["reduction_pct"] = round((1 - len(deduplicated) / max(len(memories), 1)) * 100, 1)

    return {"stats": stats, "memories": deduplicated}

raw_memories = [
    "User prefers Python for scripting",
    "User likes to use Python for automation",
    "Python is the user's preferred scripting language",
    "User is learning Rust for systems programming",
    "User has started studying Rust",
    "User works at a Berlin-based startup",
    "User is employed at a startup in Berlin",
    "User has 8 years of engineering experience",
]

result = deduplicate_memories(raw_memories, threshold=0.82)
stats = result["stats"]
print(f"Deduplication: {stats['original']} → {stats['deduplicated']} memories ({stats['reduction_pct']}% reduction)")
for m in result["memories"]:
    if m["count_merged"] > 1:
        print(f"  [merged {m['count_merged']}] {m['content']}")
    else:
        print(f"  [unique] {m['content']}")
# Expected Token Savings: 30-70% reduction depending on memory redundancy level
# Environment: Production memory systems; weekly dedup cron jobs; import pipelines
```

### Option 6: Memory Health Report and Automated Maintenance

Generate a health report for the memory store and run automated fixes: remove orphans, merge duplicates, expire stale entries.

```python
import sqlite3
import json
import time
from datetime import datetime, timedelta
from difflib import SequenceMatcher

class MemoryHealthMaintainer:
    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self._setup()
        self._seed_test_data()

    def _setup(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY,
                content TEXT,
                category TEXT,
                frequency INTEGER DEFAULT 1,
                created_ts REAL,
                last_seen_ts REAL
            )
        """)
        self.conn.commit()

    def _seed_test_data(self):
        now = time.time()
        test_data = [
            (1, "User prefers Python", "pref", 8, now - 86400*10, now - 86400),
            (2, "User likes Python best", "pref", 3, now - 86400*8, now - 86400*2),
            (3, "User works in Berlin", "fact", 5, now - 86400*30, now - 86400*5),
            (4, "User is based in Berlin Germany", "fact", 2, now - 86400*20, now - 86400*6),
            (5, "User tried Go once", "tech", 1, now - 86400*60, now - 86400*60),  # Old, rare
            (6, "User mentioned weather", "misc", 1, now - 86400*45, now - 86400*45),  # Old, misc
            (7, "User is a backend engineer", "role", 10, now - 86400*90, now - 86400),
            (8, "User does backend development", "role", 4, now - 86400*80, now - 86400*3),
        ]
        self.conn.executemany(
            "INSERT OR IGNORE INTO memories VALUES (?, ?, ?, ?, ?, ?)", test_data
        )
        self.conn.commit()

    def health_report(self) -> dict:
        now = time.time()
        rows = self.conn.execute(
            "SELECT id, content, category, frequency, created_ts, last_seen_ts FROM memories"
        ).fetchall()

        total = len(rows)
        stale = [r for r in rows if (now - r[5]) > 86400 * 30]  # Not seen in 30 days
        rare = [r for r in rows if r[3] <= 1]
        old = [r for r in rows if (now - r[4]) > 86400 * 60]

        # Near-duplicate detection
        duplicates = []
        for i, r_i in enumerate(rows):
            for j, r_j in enumerate(rows):
                if j <= i:
                    continue
                sim = SequenceMatcher(None, r_i[1].lower(), r_j[1].lower()).ratio()
                if sim > 0.75:
                    duplicates.append((r_i[0], r_j[0], round(sim, 2), r_i[1][:40], r_j[1][:40]))

        return {
            "total_memories": total,
            "stale_30d": len(stale),
            "rare_frequency_1": len(rare),
            "old_60d": len(old),
            "near_duplicates": len(duplicates),
            "duplicate_pairs": duplicates,
            "health_score": round(100 - (len(stale) + len(duplicates) * 2 + len(rare)) / max(total, 1) * 20, 1),
        }

    def run_maintenance(self, dry_run: bool = False) -> dict:
        now = time.time()
        actions = []

        # 1. Delete stale + rare memories
        stale_rare = self.conn.execute(
            "SELECT id, content FROM memories WHERE last_seen_ts < ? AND frequency <= 1",
            (now - 86400 * 45,)  # Not seen in 45 days AND mentioned only once
        ).fetchall()

        if not dry_run:
            self.conn.executemany("DELETE FROM memories WHERE id = ?", [(r[0],) for r in stale_rare])
        actions.append({"type": "deleted_stale_rare", "count": len(stale_rare),
                        "items": [r[1][:50] for r in stale_rare]})

        # 2. Flag near-duplicates for review
        report = self.health_report()
        actions.append({"type": "near_duplicates_flagged", "count": len(report["duplicate_pairs"]),
                        "pairs": [(p[3], p[4], p[2]) for p in report["duplicate_pairs"]]})

        if not dry_run:
            self.conn.commit()

        return {
            "dry_run": dry_run,
            "actions": actions,
            "health_before": report["health_score"],
        }

maintainer = MemoryHealthMaintainer()

print("=== Memory Health Report ===")
report = maintainer.health_report()
print(f"Total: {report['total_memories']} | Health score: {report['health_score']}/100")
print(f"Stale (30d): {report['stale_30d']} | Rare: {report['rare_frequency_1']} | Near-dups: {report['near_duplicates']}")
print("\nNear-duplicate pairs:")
for pair in report["duplicate_pairs"]:
    print(f"  [{pair[2]}] '{pair[3]}' ≈ '{pair[4]}'")

print("\n=== Running Maintenance ===")
result = maintainer.run_maintenance(dry_run=False)
for action in result["actions"]:
    print(f"[{action['type']}] count={action['count']}")
    if action.get("items"):
        for item in action["items"]:
            print(f"  - {item}")
# Expected Token Savings: Maintenance reduces injected memory tokens by 20-50% long-term
# Environment: Production memory systems with automated weekly maintenance
```

## Comparison Table

| Option | Mechanism | Scalability | LLM Cost | Best For |
|--------|-----------|------------|---------|----------|
| 1: Hash + String Similarity | Deterministic dedup | High | None | Real-time dedup on every write |
| 2: LLM Contradiction Detection | Model-driven resolution | Medium | Low | Resolving conflicting facts |
| 3: LLM Cluster Consolidation | Batch semantic merging | Medium | Medium | Nightly batch consolidation runs |
| 4: Frequency-Weighted Pruning | Score-based culling | Very High | None | Automated low-value memory removal |
| 5: Embedding-Based Dedup | Semantic similarity clustering | High | Embedding cost | Weekly dedup of large memory stores |
| 6: Health Report + Maintenance | Combined diagnostics + fixes | High | None | Full maintenance pipeline |
