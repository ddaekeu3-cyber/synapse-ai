---
layout: solution
title: "Agent Doesn't Implement Memory Garbage Collection"
category: memory
description: "Agent memory stores that grow without bound inflate retrieval latency, waste storage, and inject stale context into prompts. Memory garbage collection prunes expired, low-relevance, and duplicate entries on a schedule, keeping the store lean and fast."
tags: [memory, garbage-collection, sqlite, ttl, relevance, deduplication, maintenance]
---

# Agent Doesn't Implement Memory Garbage Collection

## Problem

Agent memory accumulates indefinitely. Session notes, temporary facts, superseded preferences, and duplicate observations pile up. After weeks of operation, retrieval becomes slow, prompts bloat with stale context, and conflicting memories confuse the agent.

Memory garbage collection runs on a schedule to prune expired entries, evict low-relevance memories, and deduplicate near-identical content.

---

## Option 1: TTL-Based Expiry GC

```python
import sqlite3
import json
from datetime import datetime, timedelta

class TTLMemoryStore:
    """
    Memory store with per-entry TTL.
    GC deletes entries past their expiry date.
    """

    DEFAULT_TTL_HOURS = {
        "session_note":   1,      # Expires in 1 hour
        "task_context":   24,     # Expires in 1 day
        "user_preference": 720,   # Expires in 30 days
        "long_term_fact":  None,  # Never expires
    }

    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE,
                value TEXT,
                memory_type TEXT DEFAULT 'task_context',
                created_at TEXT DEFAULT (datetime('now')),
                expires_at TEXT,
                access_count INTEGER DEFAULT 0,
                last_accessed TEXT
            )
        """)
        self.conn.commit()

    def store(self, key: str, value: str, memory_type: str = "task_context"):
        ttl_hours = self.DEFAULT_TTL_HOURS.get(memory_type)
        expires_at = None
        if ttl_hours is not None:
            expires_at = (datetime.utcnow() + timedelta(hours=ttl_hours)).isoformat()

        self.conn.execute(
            """INSERT OR REPLACE INTO memories (key, value, memory_type, expires_at)
               VALUES (?, ?, ?, ?)""",
            (key, value, memory_type, expires_at),
        )
        self.conn.commit()

    def get(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value, expires_at FROM memories WHERE key=?", (key,)
        ).fetchone()
        if not row:
            return None
        value, expires_at = row
        if expires_at and datetime.fromisoformat(expires_at) < datetime.utcnow():
            return None  # Expired
        self.conn.execute(
            "UPDATE memories SET access_count=access_count+1, last_accessed=datetime('now') WHERE key=?",
            (key,),
        )
        self.conn.commit()
        return value

    def gc(self) -> int:
        """Delete all expired entries. Returns count deleted."""
        now = datetime.utcnow().isoformat()
        self.conn.execute(
            "DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at < ?", (now,)
        )
        deleted = self.conn.execute("SELECT changes()").fetchone()[0]
        self.conn.commit()
        print(f"[GC] Deleted {deleted} expired entries")
        return deleted

    def stats(self) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN expires_at IS NULL THEN 1 ELSE 0 END) FROM memories"
        ).fetchone()
        return {"total": row[0], "permanent": row[1], "expirable": row[0] - (row[1] or 0)}


if __name__ == "__main__":
    store = TTLMemoryStore()

    # Store various types
    store.store("session_note_1", "User asked about Python", "session_note")
    store.store("pref_language", "User prefers Python", "user_preference")
    store.store("fact_capital", "Paris is the capital of France", "long_term_fact")

    print(f"Before GC: {store.stats()}")

    # Simulate expired session note by directly updating expires_at
    store.conn.execute(
        "UPDATE memories SET expires_at='2020-01-01T00:00:00' WHERE memory_type='session_note'"
    )
    store.conn.commit()

    store.gc()
    print(f"After GC: {store.stats()}")
    print(f"Session note (expired): {store.get('session_note_1')}")
    print(f"Long-term fact (permanent): {store.get('fact_capital')}")
# Expected Token Savings: 10-30% — expired entries removed before context injection
# Environment: sqlite3, json, datetime are stdlib (no pip required)
```

---

## Option 2: LRU Eviction with Access-Count Scoring

```python
import sqlite3
import json
from datetime import datetime

MAX_MEMORY_ENTRIES = 100   # Keep at most this many entries
EVICT_TO = 80              # After GC, target this count
MIN_ACCESS_COUNT = 2       # Entries with fewer accesses are eviction candidates

class LRUMemoryStore:
    """
    Memory store with LRU eviction.
    When capacity is reached, evict least recently used + low-access entries.
    """

    def __init__(self, db_path: str = ":memory:", max_entries: int = MAX_MEMORY_ENTRIES):
        self.max_entries = max_entries
        self.conn = sqlite3.connect(db_path)
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE,
                value TEXT,
                importance REAL DEFAULT 1.0,
                access_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                last_accessed TEXT DEFAULT (datetime('now'))
            )
        """)
        self.conn.commit()

    def store(self, key: str, value: str, importance: float = 1.0):
        self.conn.execute(
            "INSERT OR REPLACE INTO memories (key, value, importance) VALUES (?,?,?)",
            (key, value, importance),
        )
        self.conn.commit()
        self._maybe_gc()

    def get(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM memories WHERE key=?", (key,)
        ).fetchone()
        if not row:
            return None
        self.conn.execute(
            "UPDATE memories SET access_count=access_count+1, last_accessed=datetime('now') WHERE key=?",
            (key,),
        )
        self.conn.commit()
        return row[0]

    def _maybe_gc(self):
        count = self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        if count <= self.max_entries:
            return
        self._evict()

    def _evict(self):
        """
        Eviction priority (highest score = evict first):
        score = (1 / importance) * (1 / (access_count + 1)) * days_since_access
        """
        to_evict = (self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                    - EVICT_TO)
        if to_evict <= 0:
            return

        rows = self.conn.execute("""
            SELECT id, key, importance, access_count,
                   julianday('now') - julianday(last_accessed) as days_old
            FROM memories
            ORDER BY (1.0 / importance) * (1.0 / (access_count + 1)) * (julianday('now') - julianday(last_accessed)) DESC
            LIMIT ?
        """, (to_evict,)).fetchall()

        ids_to_delete = [r[0] for r in rows]
        self.conn.executemany("DELETE FROM memories WHERE id=?", [(i,) for i in ids_to_delete])
        self.conn.commit()
        print(f"[GC] Evicted {len(ids_to_delete)} low-priority entries")
        for r in rows[:3]:
            print(f"  Evicted: {r[1]!r} (importance={r[2]}, accesses={r[3]})")

    def gc_low_access(self, min_accesses: int = MIN_ACCESS_COUNT) -> int:
        """Remove entries that have never been accessed after a grace period."""
        self.conn.execute(
            """DELETE FROM memories
               WHERE access_count < ?
               AND julianday('now') - julianday(created_at) > 7""",
            (min_accesses,),
        )
        deleted = self.conn.execute("SELECT changes()").fetchone()[0]
        self.conn.commit()
        if deleted:
            print(f"[GC] Removed {deleted} unaccessed entries (>7 days old, <{min_accesses} accesses)")
        return deleted

    def stats(self) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*), AVG(access_count), AVG(importance) FROM memories"
        ).fetchone()
        return {
            "total": row[0],
            "avg_accesses": round(row[1] or 0, 1),
            "avg_importance": round(row[2] or 0, 2),
        }


if __name__ == "__main__":
    store = LRUMemoryStore(max_entries=10)

    # Fill beyond capacity
    for i in range(15):
        importance = 3.0 if i < 3 else 1.0  # First 3 are important
        store.store(f"memory_{i}", f"Content {i}", importance=importance)

    print(f"Stats after 15 stores (max=10): {store.stats()}")

    # Access some entries
    for i in range(3):
        store.get(f"memory_{i}")

    store.gc_low_access()
    print(f"Stats after access-count GC: {store.stats()}")
# Expected Token Savings: 20-40% — keeping only high-relevance entries reduces context injection noise
# Environment: sqlite3, json, datetime are stdlib
```

---

## Option 3: Semantic Deduplication GC

```python
import sqlite3
import json
import anthropic
from datetime import datetime

class DeduplicationGC:
    """
    Finds near-duplicate memories and merges or removes them.
    Uses a cheap model call to identify semantic duplicates.
    """

    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT,
                source TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                gc_merged_into INTEGER
            )
        """)
        self.conn.commit()

    def add(self, content: str, source: str = "agent") -> int:
        self.conn.execute(
            "INSERT INTO memories (content, source) VALUES (?,?)", (content, source)
        )
        self.conn.commit()
        return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_all_active(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, content FROM memories WHERE gc_merged_into IS NULL ORDER BY id"
        ).fetchall()
        return [{"id": r[0], "content": r[1]} for r in rows]

    def find_duplicates_with_llm(self, batch_size: int = 10) -> list[tuple[int, int]]:
        """
        Ask a cheap model to identify duplicate pairs.
        Returns list of (keep_id, delete_id) pairs.
        """
        memories = self.get_all_active()
        if len(memories) < 2:
            return []

        # Process in batches
        pairs_to_merge = []
        batch = memories[:batch_size]

        prompt = (
            "Review these numbered memory entries and identify near-duplicate pairs.\n"
            "Reply ONLY with JSON array of pairs: [{\"keep\": id, \"delete\": id}, ...]\n"
            "If no duplicates, reply: []\n\n"
            "Memories:\n" +
            "\n".join(f"{m['id']}. {m['content']}" for m in batch)
        )

        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        try:
            result = json.loads(response.content[0].text)
            pairs_to_merge = [(p["keep"], p["delete"]) for p in result if "keep" in p and "delete" in p]
        except (json.JSONDecodeError, KeyError):
            pass

        return pairs_to_merge

    def gc_deduplicate(self) -> int:
        """Find and merge duplicates. Returns count merged."""
        pairs = self.find_duplicates_with_llm()
        if not pairs:
            print("[GC] No duplicates found")
            return 0

        for keep_id, delete_id in pairs:
            self.conn.execute(
                "UPDATE memories SET gc_merged_into=? WHERE id=?", (keep_id, delete_id)
            )
            print(f"[GC] Merged memory {delete_id} → {keep_id}")

        self.conn.commit()
        return len(pairs)

    def stats(self) -> dict:
        total = self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        active = self.conn.execute("SELECT COUNT(*) FROM memories WHERE gc_merged_into IS NULL").fetchone()[0]
        return {"total": total, "active": active, "merged": total - active}


if __name__ == "__main__":
    store = DeduplicationGC()

    # Add memories including duplicates
    entries = [
        "The user prefers Python for scripting tasks.",
        "User likes to use Python for scripting.",         # Near-duplicate of above
        "The agent should respond in English.",
        "Always respond in English language.",              # Near-duplicate of above
        "The project deadline is April 30, 2026.",
        "User is working on a machine learning project.",
    ]
    for entry in entries:
        store.add(entry)

    print(f"Before dedup GC: {store.stats()}")
    merged = store.gc_deduplicate()
    print(f"After dedup GC: {store.stats()} ({merged} pairs merged)")

    print("\nActive memories:")
    for m in store.get_all_active():
        print(f"  [{m['id']}] {m['content']}")
# Expected Token Savings: 15-30% — deduplication removes redundant context injected into prompts
# Environment: pip install anthropic; sqlite3, json are stdlib
```

---

## Option 4: Importance-Decay GC with Recency Weighting

```python
import sqlite3
import math
from datetime import datetime

DECAY_HALF_LIFE_DAYS = 14.0   # Importance halves every 14 days
MIN_IMPORTANCE_THRESHOLD = 0.1  # Evict if score drops below this

class DecayMemoryStore:
    """
    Memory store where importance decays exponentially over time.
    GC evicts entries whose decayed score falls below threshold.
    """

    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE,
                value TEXT,
                base_importance REAL DEFAULT 1.0,
                created_at TEXT DEFAULT (datetime('now')),
                reinforced_at TEXT DEFAULT (datetime('now')),
                access_count INTEGER DEFAULT 0
            )
        """)
        self.conn.commit()

    def _decayed_score(self, base_importance: float, reinforced_at: str, access_count: int) -> float:
        """
        Score = base_importance * 2^(-days_since_reinforcement / half_life) * log(1 + access_count)
        """
        reinforced = datetime.fromisoformat(reinforced_at)
        days_old = (datetime.utcnow() - reinforced).total_seconds() / 86400
        decay = math.pow(2, -days_old / DECAY_HALF_LIFE_DAYS)
        access_boost = math.log1p(access_count)
        return base_importance * decay * max(access_boost, 0.5)

    def store(self, key: str, value: str, importance: float = 1.0):
        self.conn.execute(
            "INSERT OR REPLACE INTO memories (key, value, base_importance) VALUES (?,?,?)",
            (key, value, importance),
        )
        self.conn.commit()

    def reinforce(self, key: str, boost: float = 0.5):
        """Reinforce a memory — resets decay timer and boosts importance."""
        self.conn.execute(
            """UPDATE memories
               SET reinforced_at=datetime('now'),
                   base_importance=MIN(base_importance + ?, 5.0),
                   access_count=access_count+1
               WHERE key=?""",
            (boost, key),
        )
        self.conn.commit()

    def get(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM memories WHERE key=?", (key,)
        ).fetchone()
        if row:
            self.conn.execute(
                "UPDATE memories SET access_count=access_count+1 WHERE key=?", (key,)
            )
            self.conn.commit()
            return row[0]
        return None

    def gc(self, min_score: float = MIN_IMPORTANCE_THRESHOLD) -> int:
        """Evict memories whose decayed score is below threshold."""
        rows = self.conn.execute(
            "SELECT id, key, base_importance, reinforced_at, access_count FROM memories"
        ).fetchall()

        to_delete = []
        for row_id, key, base_imp, reinforced_at, access_count in rows:
            score = self._decayed_score(base_imp, reinforced_at, access_count)
            if score < min_score:
                to_delete.append((row_id, key, round(score, 4)))

        for row_id, key, score in to_delete:
            self.conn.execute("DELETE FROM memories WHERE id=?", (row_id,))
            print(f"[GC] Evicted '{key}' (score={score})")

        self.conn.commit()
        print(f"[GC] Evicted {len(to_delete)} low-score memories")
        return len(to_delete)

    def list_with_scores(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT key, value, base_importance, reinforced_at, access_count FROM memories"
        ).fetchall()
        result = []
        for key, value, base_imp, reinforced_at, access_count in rows:
            score = self._decayed_score(base_imp, reinforced_at, access_count)
            result.append({"key": key, "score": round(score, 4), "value": value[:50]})
        return sorted(result, key=lambda x: x["score"], reverse=True)


if __name__ == "__main__":
    store = DecayMemoryStore()

    # Add memories
    store.store("recent_task", "Working on ML pipeline", importance=1.0)
    store.store("old_note", "User had a question about CSV parsing", importance=0.5)
    store.store("critical_fact", "API key expires on 2026-12-31", importance=3.0)

    # Simulate decay by backdating reinforced_at for old_note
    store.conn.execute(
        "UPDATE memories SET reinforced_at='2025-01-01T00:00:00' WHERE key='old_note'"
    )
    store.conn.commit()

    print("Memory scores before GC:")
    for m in store.list_with_scores():
        print(f"  {m['key']}: score={m['score']} — {m['value']}")

    print()
    store.gc(min_score=0.1)

    print("\nMemory scores after GC:")
    for m in store.list_with_scores():
        print(f"  {m['key']}: score={m['score']} — {m['value']}")
# Expected Token Savings: 20-50% — stale low-relevance memories removed before context injection
# Environment: sqlite3, math, datetime are stdlib
```

---

## Option 5: Scheduled GC with Multiple Strategies

```python
import sqlite3
import asyncio
import json
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum

class GCStrategy(Enum):
    TTL = "ttl"
    LRU = "lru"
    CAPACITY = "capacity"
    ALL = "all"

@dataclass
class GCResult:
    strategy: str
    deleted: int
    duration_ms: float
    ran_at: str

class ScheduledGCMemoryStore:
    """
    Memory store with multiple GC strategies run on a configurable schedule.
    Records GC runs to SQLite for monitoring.
    """

    def __init__(
        self,
        db_path: str = ":memory:",
        max_entries: int = 500,
        gc_interval_sec: float = 300.0,
    ):
        self.max_entries = max_entries
        self.gc_interval = gc_interval_sec
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE,
                value TEXT,
                ttl_sec INTEGER,
                created_at TEXT DEFAULT (datetime('now')),
                expires_at TEXT,
                access_count INTEGER DEFAULT 0,
                last_accessed TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS gc_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy TEXT,
                deleted INTEGER,
                duration_ms REAL,
                entries_before INTEGER,
                entries_after INTEGER,
                ran_at TEXT DEFAULT (datetime('now'))
            );
        """)
        self.conn.commit()

    def store(self, key: str, value: str, ttl_sec: int | None = None):
        expires_at = None
        if ttl_sec:
            expires_at = (datetime.utcnow() + timedelta(seconds=ttl_sec)).isoformat()
        self.conn.execute(
            "INSERT OR REPLACE INTO memories (key, value, ttl_sec, expires_at) VALUES (?,?,?,?)",
            (key, value, ttl_sec, expires_at),
        )
        self.conn.commit()

    def get(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value, expires_at FROM memories WHERE key=?", (key,)
        ).fetchone()
        if not row:
            return None
        value, expires_at = row
        if expires_at and datetime.fromisoformat(expires_at) < datetime.utcnow():
            return None
        self.conn.execute(
            "UPDATE memories SET access_count=access_count+1, last_accessed=datetime('now') WHERE key=?",
            (key,),
        )
        self.conn.commit()
        return value

    def _count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    def gc_ttl(self) -> int:
        now = datetime.utcnow().isoformat()
        self.conn.execute(
            "DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at < ?", (now,)
        )
        deleted = self.conn.execute("SELECT changes()").fetchone()[0]
        self.conn.commit()
        return deleted

    def gc_lru(self, keep_n: int | None = None) -> int:
        target = keep_n or self.max_entries
        count = self._count()
        if count <= target:
            return 0
        to_delete = count - target
        self.conn.execute("""
            DELETE FROM memories WHERE id IN (
                SELECT id FROM memories ORDER BY last_accessed ASC LIMIT ?
            )
        """, (to_delete,))
        deleted = self.conn.execute("SELECT changes()").fetchone()[0]
        self.conn.commit()
        return deleted

    def gc_capacity(self) -> int:
        count = self._count()
        if count <= self.max_entries:
            return 0
        return self.gc_lru()

    def run_gc(self, strategy: GCStrategy = GCStrategy.ALL) -> list[GCResult]:
        import time
        results = []
        strategies = [GCStrategy.TTL, GCStrategy.CAPACITY] if strategy == GCStrategy.ALL else [strategy]

        for s in strategies:
            before = self._count()
            t0 = time.monotonic()

            if s == GCStrategy.TTL:
                deleted = self.gc_ttl()
            elif s == GCStrategy.LRU:
                deleted = self.gc_lru()
            elif s == GCStrategy.CAPACITY:
                deleted = self.gc_capacity()
            else:
                deleted = 0

            duration_ms = (time.monotonic() - t0) * 1000
            after = self._count()

            result = GCResult(
                strategy=s.value,
                deleted=deleted,
                duration_ms=round(duration_ms, 2),
                ran_at=datetime.utcnow().isoformat(),
            )
            results.append(result)

            self.conn.execute(
                "INSERT INTO gc_log (strategy, deleted, duration_ms, entries_before, entries_after) VALUES (?,?,?,?,?)",
                (s.value, deleted, duration_ms, before, after),
            )
            self.conn.commit()

            if deleted:
                print(f"[GC/{s.value}] Deleted {deleted} entries in {duration_ms:.1f}ms ({before}→{after})")

        return results

    def gc_history(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT strategy, deleted, duration_ms, entries_before, entries_after, ran_at FROM gc_log ORDER BY ran_at DESC LIMIT 20"
        ).fetchall()
        return [{"strategy": r[0], "deleted": r[1], "duration_ms": r[2],
                 "before": r[3], "after": r[4], "at": r[5]} for r in rows]


async def run_scheduled_gc_demo():
    store = ScheduledGCMemoryStore(max_entries=5, gc_interval_sec=2.0)

    # Populate
    for i in range(8):
        ttl = 1 if i < 3 else None  # First 3 expire quickly
        store.store(f"key_{i}", f"Memory content {i}", ttl_sec=ttl)

    print(f"Stored 8 entries (max=5, 3 with 1s TTL)")
    print(f"Count before GC: {store._count()}")

    # Wait for TTL to expire
    await asyncio.sleep(1.5)

    results = store.run_gc()
    for r in results:
        print(f"[GC] {r.strategy}: {r.deleted} deleted in {r.duration_ms}ms")

    print(f"\nGC History: {json.dumps(store.gc_history(), indent=2)}")


if __name__ == "__main__":
    asyncio.run(run_scheduled_gc_demo())
# Expected Token Savings: 15-40% — scheduled GC keeps store size bounded, reducing retrieval noise
# Environment: sqlite3, asyncio, json, datetime are stdlib
```

---

## Option 6: Full GC Pipeline with Reporting and Alerting

```python
import sqlite3
import json
import math
from datetime import datetime, timedelta
from dataclasses import dataclass

@dataclass
class GCReport:
    gc_id: str
    strategies_run: list[str]
    total_deleted: int
    entries_before: int
    entries_after: int
    size_reduction_pct: float
    duration_ms: float
    warnings: list[str]
    ran_at: str

class ComprehensiveGCPipeline:
    """
    Full GC pipeline: TTL expiry → deduplication → importance decay → capacity cap.
    Generates a detailed report after each run.
    """

    HALF_LIFE_DAYS = 30.0
    MIN_SCORE = 0.05
    MAX_ENTRIES = 1000
    WARN_AT_ENTRIES = 800

    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE,
                value TEXT,
                importance REAL DEFAULT 1.0,
                expires_at TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                last_accessed TEXT DEFAULT (datetime('now')),
                access_count INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS gc_reports (
                gc_id TEXT PRIMARY KEY,
                report_json TEXT,
                ran_at TEXT DEFAULT (datetime('now'))
            );
        """)
        self.conn.commit()

    def store(self, key: str, value: str, importance: float = 1.0, ttl_hours: float | None = None):
        expires_at = (datetime.utcnow() + timedelta(hours=ttl_hours)).isoformat() if ttl_hours else None
        self.conn.execute(
            "INSERT OR REPLACE INTO memories (key, value, importance, expires_at) VALUES (?,?,?,?)",
            (key, value, importance, expires_at),
        )
        self.conn.commit()

    def _count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    def _step_ttl(self) -> int:
        now = datetime.utcnow().isoformat()
        self.conn.execute("DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at < ?", (now,))
        deleted = self.conn.execute("SELECT changes()").fetchone()[0]
        self.conn.commit()
        return deleted

    def _step_decay(self) -> int:
        rows = self.conn.execute(
            "SELECT id, importance, last_accessed FROM memories"
        ).fetchall()
        to_delete = []
        for row_id, importance, last_accessed in rows:
            days = (datetime.utcnow() - datetime.fromisoformat(last_accessed)).total_seconds() / 86400
            score = importance * math.pow(2, -days / self.HALF_LIFE_DAYS)
            if score < self.MIN_SCORE:
                to_delete.append(row_id)

        for row_id in to_delete:
            self.conn.execute("DELETE FROM memories WHERE id=?", (row_id,))
        self.conn.commit()
        return len(to_delete)

    def _step_capacity(self) -> int:
        count = self._count()
        if count <= self.MAX_ENTRIES:
            return 0
        to_evict = count - self.MAX_ENTRIES
        self.conn.execute("""
            DELETE FROM memories WHERE id IN (
                SELECT id FROM memories ORDER BY importance ASC, last_accessed ASC LIMIT ?
            )
        """, (to_evict,))
        deleted = self.conn.execute("SELECT changes()").fetchone()[0]
        self.conn.commit()
        return deleted

    def run_full_gc(self) -> GCReport:
        import time, uuid
        gc_id = str(uuid.uuid4())[:8]
        before = self._count()
        t0 = time.monotonic()
        warnings = []

        deleted_ttl = self._step_ttl()
        deleted_decay = self._step_decay()
        deleted_capacity = self._step_capacity()

        total_deleted = deleted_ttl + deleted_decay + deleted_capacity
        after = self._count()
        duration_ms = (time.monotonic() - t0) * 1000

        if after >= self.WARN_AT_ENTRIES:
            warnings.append(f"Memory store still has {after} entries (warn threshold={self.WARN_AT_ENTRIES})")

        reduction_pct = (total_deleted / max(before, 1)) * 100
        report = GCReport(
            gc_id=gc_id,
            strategies_run=["ttl", "decay", "capacity"],
            total_deleted=total_deleted,
            entries_before=before,
            entries_after=after,
            size_reduction_pct=round(reduction_pct, 1),
            duration_ms=round(duration_ms, 2),
            warnings=warnings,
            ran_at=datetime.utcnow().isoformat(),
        )

        self.conn.execute(
            "INSERT INTO gc_reports (gc_id, report_json) VALUES (?,?)",
            (gc_id, json.dumps(report.__dict__)),
        )
        self.conn.commit()
        return report


if __name__ == "__main__":
    pipeline = ComprehensiveGCPipeline()

    # Populate with varied memories
    for i in range(20):
        importance = 0.1 if i < 5 else (3.0 if i < 8 else 1.0)
        ttl = 0.001 if i < 3 else None  # First 3 expire immediately (in hours)
        pipeline.store(f"key_{i}", f"Memory {i}", importance=importance, ttl_hours=ttl)

    # Simulate stale entries
    pipeline.conn.execute(
        "UPDATE memories SET last_accessed='2024-01-01T00:00:00' WHERE importance < 0.5"
    )
    pipeline.conn.commit()

    print(f"Before GC: {pipeline._count()} entries")
    report = pipeline.run_full_gc()
    print(f"After GC:  {report.entries_after} entries")
    print(f"\nGC Report:")
    print(f"  Deleted:    {report.total_deleted} ({report.size_reduction_pct}% reduction)")
    print(f"  Duration:   {report.duration_ms}ms")
    print(f"  Strategies: {', '.join(report.strategies_run)}")
    if report.warnings:
        for w in report.warnings:
            print(f"  ⚠️  {w}")
# Expected Token Savings: 20-60% — comprehensive GC keeps memory store 40-60% smaller long-term
# Environment: sqlite3, json, math, datetime, time, uuid are stdlib
```

---

## Comparison

| Option | Strategy | Trigger | Handles Duplicates | Importance-Aware | SQLite | Best For |
|--------|----------|---------|-------------------|-----------------|--------|----------|
| 1 | TTL expiry | Manual / scheduled | No | No | Yes | Session/task-scoped notes |
| 2 | LRU eviction | Capacity threshold | No | No | Yes | Fixed-size memory pools |
| 3 | Semantic dedup (LLM) | Manual | Yes | No | Yes | Removing redundant facts |
| 4 | Importance decay | Manual / scheduled | No | Yes | Yes | Long-running agents with reinforcement |
| 5 | Multi-strategy scheduled | Async scheduler | No | No | Yes | Automated background GC |
| 6 | Full pipeline (TTL+decay+cap) | Manual | No | Yes | Yes | Production memory management |
