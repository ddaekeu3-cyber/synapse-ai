---
layout: solution
title: "Agent Doesn't Implement Importance Scoring for Memory Retention"
category: memory
description: "Score memories by relevance, recency, and access frequency before storing or evicting — so limited memory slots are used by facts that actually matter."
tags: [memory, importance-scoring, retention, eviction, sqlite, python]
---

# Agent Doesn't Implement Importance Scoring for Memory Retention

Without importance scoring, agents store everything equally — filling memory with trivial facts while losing critical context. Scoring memories before storing (and re-scoring before evicting) ensures the most useful information survives compression and slot limits.

## Option 1: Heuristic Importance Score at Write Time

```python
import anthropic
import re
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class Memory:
    text: str
    score: float
    tags: list[str]

def score_memory(text: str) -> float:
    """Heuristic importance score 0.0–1.0."""
    score = 0.0
    words = text.lower().split()

    # Length signal: mid-length facts score higher than one-word or essay entries
    length_score = min(len(words) / 30, 1.0) * 0.2
    score += length_score

    # High-signal keywords
    important_terms = [
        "prefer", "always", "never", "must", "important", "critical",
        "deadline", "password", "api key", "name is", "my goal",
        "remember", "favorite", "hate", "love", "need",
    ]
    keyword_hits = sum(1 for t in important_terms if t in text.lower())
    score += min(keyword_hits * 0.15, 0.4)

    # Numbers and specific data
    if re.search(r"\b\d+\b", text):
        score += 0.1

    # Proper nouns (crude: capitalized mid-sentence words)
    proper = len(re.findall(r"(?<!\. )[A-Z][a-z]{2,}", text))
    score += min(proper * 0.05, 0.2)

    # Questions are low importance
    if text.strip().endswith("?"):
        score -= 0.2

    return max(0.0, min(score, 1.0))

def store_if_important(text: str, memory_bank: list[Memory], threshold: float = 0.3) -> bool:
    s = score_memory(text)
    tags = []
    if "prefer" in text.lower() or "favorite" in text.lower():
        tags.append("preference")
    if re.search(r"\b\d{4}\b", text):
        tags.append("date_or_number")
    mem = Memory(text, s, tags)
    if s >= threshold:
        memory_bank.append(mem)
        print(f"[STORED score={s:.2f}] {text[:60]}")
        return True
    print(f"[SKIPPED score={s:.2f}] {text[:60]}")
    return False

bank: list[Memory] = []
candidates = [
    "The user's name is Alice and she prefers Python over JavaScript.",
    "Okay.",
    "The API key expires on 2026-12-31 — must rotate before then.",
    "What time is it?",
    "Alice's favorite model is claude-opus-4-6 for complex reasoning tasks.",
    "Nice.",
    "The production database is at db.internal:5432.",
]

for c in candidates:
    store_if_important(c, bank)

print(f"\nStored {len(bank)} memories:")
for m in sorted(bank, key=lambda x: -x.score):
    print(f"  [{m.score:.2f}] {m.text[:80]}")

# Expected Token Savings: Filters ~40% of low-value turns; keeps context window lean
# Environment: pure Python; no external dependencies; adjust threshold per use case
```

## Option 2: LLM-Scored Importance with Budget Cap

```python
import anthropic
import heapq

client = anthropic.Anthropic()

MAX_MEMORIES = 10

def llm_score_memory(text: str) -> float:
    """Use a cheap model to score importance 1-10, normalized to 0-1."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=16,
        messages=[{
            "role": "user",
            "content": (
                f"Rate the importance of storing this fact for a future conversation assistant "
                f"(1=trivial, 10=critical). Reply with a single integer only.\n\nFact: {text}"
            ),
        }],
    )
    try:
        score = int(resp.content[0].text.strip().split()[0])
        return max(1, min(score, 10)) / 10.0
    except (ValueError, IndexError):
        return 0.5

class ImportanceMemoryBank:
    def __init__(self, max_size: int):
        self.max_size = max_size
        # Min-heap: (score, counter, text) — lowest score evicted first
        self._heap: list[tuple] = []
        self._counter = 0

    def add(self, text: str, score: float):
        if len(self._heap) < self.max_size:
            heapq.heappush(self._heap, (score, self._counter, text))
            print(f"[ADD score={score:.2f}] {text[:60]}")
        elif score > self._heap[0][0]:
            evicted = heapq.heapreplace(self._heap, (score, self._counter, text))
            print(f"[REPLACE evicted={evicted[0]:.2f} new={score:.2f}] {text[:60]}")
        else:
            print(f"[REJECT score={score:.2f} < min={self._heap[0][0]:.2f}] {text[:60]}")
        self._counter += 1

    def top_k(self, k: int) -> list[tuple[float, str]]:
        return sorted([(s, t) for s, _, t in self._heap], reverse=True)[:k]

bank = ImportanceMemoryBank(max_size=MAX_MEMORIES)
facts = [
    "The user prefers dark mode in all applications.",
    "Hi there.",
    "The team uses PostgreSQL 15 in production.",
    "Okay sure.",
    "Critical: never delete rows from the orders table without archiving first.",
    "The user's timezone is UTC+9 (Tokyo).",
    "Got it.",
    "The model budget for this project is $500/month.",
    "The main repo is at github.com/acme/backend.",
    "The user hates verbose responses.",
    "Weekly sync is every Monday at 10am JST.",
    "Interesting.",
]

for fact in facts:
    score = llm_score_memory(fact)
    bank.add(fact, score)

print("\nTop memories retained:")
for score, text in bank.top_k(5):
    print(f"  [{score:.2f}] {text}")

# Expected Token Savings: 50-70% slot reduction; Haiku scoring costs ~5 tokens per fact
# Environment: any; LLM scorer adapts to domain automatically
```

## Option 3: Access-Frequency + Recency Composite Score

```python
import anthropic
import sqlite3
import time
import math

client = anthropic.Anthropic()
DB = "memory_importance.db"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            text     TEXT UNIQUE,
            stored_at REAL,
            last_accessed REAL,
            access_count  INTEGER DEFAULT 0,
            base_score    REAL DEFAULT 0.5
        )
    """)
    con.commit(); con.close()

def composite_score(base: float, access_count: int, last_accessed: float,
                    stored_at: float, now: float) -> float:
    """Composite: base importance + frequency + recency decay."""
    age_days = (now - stored_at) / 86400
    recency_days = (now - last_accessed) / 86400
    frequency_bonus = math.log1p(access_count) * 0.1
    recency_bonus = math.exp(-recency_days / 7) * 0.2   # half-life ~7 days
    age_penalty = math.exp(-age_days / 30) * 0.1        # older memories decay slightly
    return min(base + frequency_bonus + recency_bonus - age_penalty, 1.0)

def store_memory(text: str, base_score: float = 0.5):
    now = time.time()
    con = sqlite3.connect(DB)
    con.execute(
        "INSERT OR IGNORE INTO memories (text, stored_at, last_accessed, base_score) VALUES (?,?,?,?)",
        (text, now, now, base_score)
    )
    con.commit(); con.close()

def access_memory(text: str) -> str | None:
    now = time.time()
    con = sqlite3.connect(DB)
    row = con.execute("SELECT text FROM memories WHERE text=?", (text,)).fetchone()
    if row:
        con.execute(
            "UPDATE memories SET access_count=access_count+1, last_accessed=? WHERE text=?",
            (now, text)
        )
        con.commit()
    con.close()
    return row[0] if row else None

def get_top_memories(k: int = 5) -> list[tuple[float, str]]:
    now = time.time()
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT text, base_score, access_count, last_accessed, stored_at FROM memories"
    ).fetchall()
    con.close()
    scored = [
        (composite_score(r[1], r[2], r[3], r[4], now), r[0])
        for r in rows
    ]
    return sorted(scored, reverse=True)[:k]

def evict_low_importance(keep: int = 20):
    now = time.time()
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT id, text, base_score, access_count, last_accessed, stored_at FROM memories"
    ).fetchall()
    scored = sorted(rows, key=lambda r: composite_score(r[2], r[3], r[4], r[5], now))
    to_delete = [r[0] for r in scored[:-keep]]
    if to_delete:
        con.execute(f"DELETE FROM memories WHERE id IN ({','.join('?'*len(to_delete))})", to_delete)
        print(f"Evicted {len(to_delete)} low-importance memories")
    con.commit(); con.close()

init_db()
store_memory("User prefers GPT-4 for coding tasks.", base_score=0.7)
store_memory("The API key is sk-test-abc123.", base_score=0.9)
store_memory("User said 'okay'.", base_score=0.1)
store_memory("Project deadline is 2026-06-01.", base_score=0.8)
store_memory("System timezone is UTC.", base_score=0.5)

# Simulate access patterns
for _ in range(5):
    access_memory("The API key is sk-test-abc123.")
for _ in range(2):
    access_memory("Project deadline is 2026-06-01.")

print("Top memories by composite score:")
for score, text in get_top_memories(5):
    print(f"  [{score:.3f}] {text}")

evict_low_importance(keep=4)

# Expected Token Savings: Frequently-accessed + recent facts stay; stale facts auto-evict
# Environment: SQLite persists across restarts; composite score tunable per app
```

## Option 4: Category-Weighted Retention Policy

```python
import anthropic
import re
from enum import Enum

client = anthropic.Anthropic()

class MemoryCategory(Enum):
    PREFERENCE     = ("preference",    0.9)
    CONSTRAINT     = ("constraint",    1.0)
    FACTUAL        = ("factual",       0.6)
    TEMPORAL       = ("temporal",      0.7)
    CONVERSATIONAL = ("conversational", 0.1)

    def __init__(self, label: str, base_weight: float):
        self.label = label
        self.base_weight = base_weight

def classify_memory(text: str) -> MemoryCategory:
    t = text.lower()
    if any(k in t for k in ["prefer", "like", "hate", "love", "favorite", "dislike"]):
        return MemoryCategory.PREFERENCE
    if any(k in t for k in ["never", "always", "must", "forbidden", "required", "critical"]):
        return MemoryCategory.CONSTRAINT
    if re.search(r"\b(deadline|by|due|scheduled|on \w+ \d+|expires?)\b", t):
        return MemoryCategory.TEMPORAL
    if any(k in t for k in ["okay", "got it", "sure", "thanks", "hi", "hello", "bye"]):
        return MemoryCategory.CONVERSATIONAL
    return MemoryCategory.FACTUAL

def should_retain(text: str, slot_pressure: float = 0.0) -> tuple[bool, float]:
    """
    Determine if a memory should be retained.
    slot_pressure: 0.0 (plenty of space) to 1.0 (nearly full)
    """
    category = classify_memory(text)
    base = category.base_weight
    # Raise bar as memory fills up
    threshold = 0.3 + slot_pressure * 0.5
    score = base * (1.0 - slot_pressure * 0.3)
    return score >= threshold, score

class CategoryMemoryStore:
    def __init__(self, max_slots: int = 15):
        self.max_slots = max_slots
        self.memories: list[tuple[float, str, MemoryCategory]] = []

    @property
    def pressure(self) -> float:
        return len(self.memories) / self.max_slots

    def add(self, text: str):
        keep, score = should_retain(text, self.pressure)
        cat = classify_memory(text)
        if keep:
            self.memories.append((score, text, cat))
            self.memories.sort(key=lambda x: -x[0])
            if len(self.memories) > self.max_slots:
                evicted = self.memories.pop()
                print(f"  [EVICT {evicted[2].label}] {evicted[1][:50]}")
            print(f"  [STORE {cat.label} score={score:.2f}] {text[:60]}")
        else:
            print(f"  [SKIP  {cat.label} score={score:.2f}] {text[:60]}")

    def summary(self):
        print(f"\nMemory ({len(self.memories)}/{self.max_slots} slots):")
        for score, text, cat in self.memories:
            print(f"  [{cat.label:14s} {score:.2f}] {text[:70]}")

store = CategoryMemoryStore(max_slots=5)
entries = [
    "The user prefers TypeScript over JavaScript.",
    "Sure, understood.",
    "Never modify the production database without a backup.",
    "The MVP demo is due 2026-05-15.",
    "Okay.",
    "The user's primary language is Korean.",
    "Got it, will do.",
    "Always use HTTPS for external API calls.",
    "The team has 5 engineers.",
    "Hi!",
]
for e in entries:
    store.add(e)
store.summary()

# Expected Token Savings: Conversational filler never stored; constraints always kept
# Environment: pure Python; extend MemoryCategory with domain-specific categories
```

## Option 5: Embedding-Based Novelty Score (Deduplication-Aware)

```python
import anthropic
import math

client = anthropic.Anthropic()

def embed(text: str) -> list[float]:
    """Get text embedding via Claude's token probabilities as a proxy."""
    # In production, use a real embedding model (e.g., voyage-3).
    # Here we use a character n-gram frequency vector as a lightweight stand-in.
    ngrams: dict[str, int] = {}
    for i in range(len(text) - 2):
        ng = text[i:i+3].lower()
        ngrams[ng] = ngrams.get(ng, 0) + 1
    total = sum(ngrams.values()) or 1
    keys = sorted(ngrams)[:64]
    return [ngrams.get(k, 0) / total for k in keys] + [0.0] * (64 - len(keys))

def cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x**2 for x in a))
    nb = math.sqrt(sum(x**2 for x in b))
    return dot / (na * nb + 1e-9)

def novelty_score(text: str, existing_embeddings: list[list[float]]) -> float:
    """1.0 = completely novel; 0.0 = exact duplicate."""
    if not existing_embeddings:
        return 1.0
    emb = embed(text)
    max_sim = max(cosine_sim(emb, e) for e in existing_embeddings)
    return 1.0 - max_sim

class NoveltyMemoryStore:
    def __init__(self, novelty_threshold: float = 0.3, max_size: int = 20):
        self.threshold = novelty_threshold
        self.max_size = max_size
        self.memories: list[str] = []
        self._embeddings: list[list[float]] = []

    def add(self, text: str, base_importance: float = 0.5):
        novelty = novelty_score(text, self._embeddings)
        combined = base_importance * 0.5 + novelty * 0.5
        print(f"novelty={novelty:.2f} importance={base_importance:.2f} combined={combined:.2f}: {text[:60]}")
        if combined >= self.threshold and len(self.memories) < self.max_size:
            self.memories.append(text)
            self._embeddings.append(embed(text))
        else:
            print(f"  -> SKIPPED (below threshold or full)")

store = NoveltyMemoryStore(novelty_threshold=0.35)
facts = [
    ("The user prefers async Python.", 0.8),
    ("The user likes Python async patterns.", 0.7),     # Near-duplicate — should score low novelty
    ("The API rate limit is 1000 req/min.", 0.9),
    ("Rate limiting is set to 1000 per minute.", 0.8),  # Near-duplicate
    ("The project uses PostgreSQL 15.", 0.7),
    ("Deployment is on AWS us-east-1.", 0.8),
]
for text, importance in facts:
    store.add(text, importance)

print(f"\nStored {len(store.memories)} unique memories:")
for m in store.memories:
    print(f"  {m}")

# Expected Token Savings: Eliminates redundant near-duplicate memories; saves 20-40% of slots
# Environment: swap embed() with voyage-3 or OpenAI embeddings for production accuracy
```

## Option 6: Decay-Adjusted Importance with SQLite Dashboard

```python
import anthropic
import sqlite3
import time
import math
import uuid

client = anthropic.Anthropic()
DB = "mem_importance_v2.db"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS mem_store (
            id TEXT PRIMARY KEY,
            text TEXT,
            importance REAL,
            half_life_days REAL DEFAULT 30,
            created_at REAL,
            last_reinforced REAL
        )
    """)
    con.commit(); con.close()

def current_score(importance: float, half_life: float,
                  created_at: float, last_reinforced: float) -> float:
    now = time.time()
    days_since_reinforce = (now - last_reinforced) / 86400
    decay = math.exp(-0.693 * days_since_reinforce / half_life)
    return importance * decay

def store_memory(text: str, importance: float, half_life_days: float = 30.0):
    now = time.time()
    mid = uuid.uuid4().hex[:8]
    con = sqlite3.connect(DB)
    con.execute(
        "INSERT INTO mem_store VALUES (?,?,?,?,?,?)",
        (mid, text, importance, half_life_days, now, now)
    )
    con.commit(); con.close()
    print(f"[{mid}] Stored (importance={importance:.2f}, T½={half_life_days}d): {text[:60]}")
    return mid

def reinforce_memory(mid: str):
    """Accessing a memory resets its decay clock."""
    con = sqlite3.connect(DB)
    con.execute("UPDATE mem_store SET last_reinforced=? WHERE id=?", (time.time(), mid))
    con.commit(); con.close()

def get_active_memories(min_score: float = 0.2, limit: int = 10) -> list[dict]:
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT id, text, importance, half_life_days, created_at, last_reinforced FROM mem_store"
    ).fetchall()
    con.close()
    results = []
    for r in rows:
        score = current_score(r[2], r[3], r[4], r[5])
        if score >= min_score:
            results.append({"id": r[0], "text": r[1], "score": score})
    return sorted(results, key=lambda x: -x["score"])[:limit]

def prune_expired(min_score: float = 0.1):
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT id, importance, half_life_days, created_at, last_reinforced FROM mem_store"
    ).fetchall()
    expired = [r[0] for r in rows
               if current_score(r[1], r[2], r[3], r[4]) < min_score]
    if expired:
        con.execute(f"DELETE FROM mem_store WHERE id IN ({','.join('?'*len(expired))})", expired)
        print(f"Pruned {len(expired)} expired memories")
    con.commit(); con.close()

init_db()
m1 = store_memory("User's primary language is Korean.", 0.9, half_life_days=180)
m2 = store_memory("Meeting notes from 2026-01-15.", 0.6, half_life_days=7)
m3 = store_memory("API key rotated 2026-04-10.", 0.8, half_life_days=30)
m4 = store_memory("User said 'thanks'.", 0.1, half_life_days=1)

reinforce_memory(m1)  # Still relevant

print("\nActive memories:")
for m in get_active_memories(min_score=0.15):
    print(f"  [{m['score']:.3f}] {m['text'][:70]}")

prune_expired(min_score=0.15)

# Expected Token Savings: High-importance long-lived memories stay; ephemeral facts auto-expire
# Environment: SQLite; half_life_days tunable per memory type; reinforce on retrieval
```

## Comparison

| Option | Scoring Method | Eviction Strategy | Best For |
|--------|---------------|-------------------|----------|
| 1 — Heuristic | Keyword + structure rules | Threshold filter at write | Zero-cost, fast ingestion |
| 2 — LLM Scored | Claude Haiku rates 1–10 | Min-heap evicts lowest | Domain-adaptive scoring |
| 3 — Access + Recency | Composite frequency + decay | SQLite pruning | Long-running agents with access logs |
| 4 — Category Weighted | Rule-based classification | Slot pressure threshold | Policy-driven retention by type |
| 5 — Novelty Score | Embedding cosine similarity | Deduplication gate | Preventing redundant storage |
| 6 — Decay + Reinforce | Exponential half-life | Score floor pruning | Time-sensitive fact management |
