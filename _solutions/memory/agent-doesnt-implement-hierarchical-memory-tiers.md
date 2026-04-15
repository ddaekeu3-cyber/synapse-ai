---
layout: solution
title: "Agent Doesn't Implement Hierarchical Memory Tiers"
category: memory
description: "Separate working memory (current turn), episodic memory (recent sessions), and semantic memory (persistent facts) so the agent retrieves the right information at the right granularity."
tags: [memory, hierarchical, working-memory, episodic, semantic, sqlite, python]
---

# Agent Doesn't Implement Hierarchical Memory Tiers

A single flat memory store forces a painful tradeoff: keep everything and overflow context, or compress everything and lose detail. Hierarchical tiers solve this by giving each type of information its appropriate lifetime, storage, and retrieval path.

## Option 1: Three-Tier Architecture (Working / Episodic / Semantic)

```python
import anthropic
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class WorkingMemory:
    """Current turn scratch-pad — cleared after each response."""
    slots: dict[str, str] = field(default_factory=dict)
    max_slots: int = 8

    def set(self, key: str, value: str):
        if len(self.slots) >= self.max_slots:
            oldest = next(iter(self.slots))
            del self.slots[oldest]
        self.slots[key] = value

    def get_all(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.slots.items())

    def clear(self):
        self.slots.clear()

@dataclass
class EpisodicMemory:
    """Session-scoped events — what happened in recent conversations."""
    episodes: list[dict] = field(default_factory=list)
    max_episodes: int = 20

    def record(self, summary: str, ts: float | None = None):
        self.episodes.append({"summary": summary, "ts": ts or time.time()})
        if len(self.episodes) > self.max_episodes:
            self.episodes.pop(0)

    def recent(self, n: int = 5) -> list[str]:
        return [e["summary"] for e in self.episodes[-n:]]

@dataclass
class SemanticMemory:
    """Long-term facts — persists across sessions."""
    facts: dict[str, str] = field(default_factory=dict)

    def store(self, key: str, value: str):
        self.facts[key] = value

    def retrieve(self, query: str) -> list[str]:
        q = query.lower()
        return [v for k, v in self.facts.items()
                if any(w in k.lower() or w in v.lower() for w in q.split())]

# ── Agent using all three tiers ───────────────────────────────────────────

def build_context(
    working: WorkingMemory,
    episodic: EpisodicMemory,
    semantic: SemanticMemory,
    user_input: str,
) -> str:
    semantic_hits = semantic.retrieve(user_input)[:3]
    recent_episodes = episodic.recent(3)
    parts = []
    if semantic_hits:
        parts.append("Facts: " + "; ".join(semantic_hits))
    if recent_episodes:
        parts.append("Recent: " + "; ".join(recent_episodes))
    if working.slots:
        parts.append("Current: " + working.get_all())
    return "\n".join(parts)

def respond(user_input: str, working: WorkingMemory,
            episodic: EpisodicMemory, semantic: SemanticMemory) -> str:
    context = build_context(working, episodic, semantic, user_input)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=f"You are a helpful assistant.\n\nMemory context:\n{context}" if context else
               "You are a helpful assistant.",
        messages=[{"role": "user", "content": user_input}],
    )
    result = resp.content[0].text
    working.set("last_topic", user_input[:40])
    episodic.record(f"User asked: {user_input[:50]}. Agent replied: {result[:50]}")
    working.clear()
    return result

# Bootstrap semantic memory
sem = SemanticMemory()
sem.store("user_name", "The user's name is Alice.")
sem.store("user_lang", "The user prefers Python.")
sem.store("timezone",  "User is in UTC+9 (Tokyo).")

epi = EpisodicMemory()
wkm = WorkingMemory()

for q in ["What's my name?", "What language do I prefer?", "What time zone am I in?"]:
    print(f"User: {q}")
    print(f"Agent: {respond(q, wkm, epi, sem)}\n")

# Expected Token Savings: Only relevant semantic facts injected; episodic limited to 3 recent turns
# Environment: pure Python; swap SemanticMemory.facts with SQLite for persistence
```

## Option 2: SQLite-Backed Tiered Memory with Auto-Promotion

```python
import anthropic
import sqlite3
import time
import uuid

client = anthropic.Anthropic()
DB = "hierarchical_mem.db"

def init_db():
    con = sqlite3.connect(DB)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS working (
            session_id TEXT, key TEXT, value TEXT,
            PRIMARY KEY (session_id, key)
        );
        CREATE TABLE IF NOT EXISTS episodic (
            id TEXT PRIMARY KEY, session_id TEXT,
            summary TEXT, ts REAL, promoted INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS semantic (
            key TEXT PRIMARY KEY, value TEXT,
            importance REAL DEFAULT 0.5, access_count INTEGER DEFAULT 0
        );
    """)
    con.commit(); con.close()

def working_set(session: str, key: str, value: str):
    con = sqlite3.connect(DB)
    con.execute("INSERT OR REPLACE INTO working VALUES (?,?,?)", (session, key, value))
    con.commit(); con.close()

def working_get_all(session: str) -> dict:
    con = sqlite3.connect(DB)
    rows = con.execute("SELECT key, value FROM working WHERE session_id=?", (session,)).fetchall()
    con.close()
    return dict(rows)

def working_clear(session: str):
    con = sqlite3.connect(DB)
    con.execute("DELETE FROM working WHERE session_id=?", (session,))
    con.commit(); con.close()

def episodic_record(session: str, summary: str):
    con = sqlite3.connect(DB)
    con.execute("INSERT INTO episodic VALUES (?,?,?,?,0)",
                (uuid.uuid4().hex[:8], session, summary, time.time()))
    # Keep only last 30 episodes per session
    con.execute("""
        DELETE FROM episodic WHERE session_id=? AND id NOT IN (
            SELECT id FROM episodic WHERE session_id=? ORDER BY ts DESC LIMIT 30
        )
    """, (session, session))
    con.commit(); con.close()

def episodic_recent(session: str, n: int = 5) -> list[str]:
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT summary FROM episodic WHERE session_id=? ORDER BY ts DESC LIMIT ?",
        (session, n)
    ).fetchall()
    con.close()
    return [r[0] for r in reversed(rows)]

def semantic_store(key: str, value: str, importance: float = 0.5):
    con = sqlite3.connect(DB)
    con.execute("INSERT OR REPLACE INTO semantic VALUES (?,?,?,0)", (key, value, importance))
    con.commit(); con.close()

def semantic_search(query: str, limit: int = 3) -> list[str]:
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT key, value FROM semantic ORDER BY importance DESC, access_count DESC"
    ).fetchall()
    q = query.lower()
    hits = [v for k, v in rows if any(w in (k+v).lower() for w in q.split())][:limit]
    if hits:
        con.execute("UPDATE semantic SET access_count=access_count+1 WHERE key IN ({})".format(
            ",".join("?" * len(hits))
        ), hits)
        con.commit()
    con.close()
    return hits

def auto_promote_episodic(session: str):
    """Promote frequently-repeated episodic themes to semantic memory."""
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT summary FROM episodic WHERE session_id=? AND promoted=0 ORDER BY ts DESC LIMIT 10",
        (session,)
    ).fetchall()
    con.close()
    for (summary,) in rows:
        if "prefers" in summary.lower() or "always" in summary.lower():
            key = f"auto_{abs(hash(summary)) % 10000}"
            semantic_store(key, summary[:200], importance=0.7)
            con = sqlite3.connect(DB)
            con.execute("UPDATE episodic SET promoted=1 WHERE summary=?", (summary,))
            con.commit(); con.close()
            print(f"  [PROMOTED] {summary[:60]}")

init_db()
SESSION = uuid.uuid4().hex[:8]

# Seed semantic memory
semantic_store("user_name",     "User's name is Bob.", importance=0.9)
semantic_store("user_pref_lang","Bob prefers TypeScript.", importance=0.8)

for q in ["What's my name?", "What language do I use?", "Explain async/await in TypeScript"]:
    ctx_parts = []
    sem = semantic_search(q, 2)
    if sem: ctx_parts.append("Facts: " + "; ".join(sem))
    epi = episodic_recent(SESSION, 3)
    if epi: ctx_parts.append("Recent: " + "; ".join(epi))
    wkm = working_get_all(SESSION)
    if wkm: ctx_parts.append("Working: " + str(wkm))

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system="You are a concise assistant.\n\n" + "\n".join(ctx_parts) if ctx_parts else
               "You are a concise assistant.",
        messages=[{"role": "user", "content": q}],
    )
    result = resp.content[0].text
    working_set(SESSION, "last_q", q[:40])
    episodic_record(SESSION, f"Asked: {q[:50]}. Got: {result[:50]}")
    working_clear(SESSION)
    print(f"Q: {q}\nA: {result[:80]}\n")

auto_promote_episodic(SESSION)

# Expected Token Savings: Only relevant semantic + 3 recent episodes injected per turn
# Environment: SQLite persists all tiers; auto-promotion distills episodes into semantic facts
```

## Option 3: Token-Budget-Aware Tier Selection

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class TieredMemoryWithBudget:
    """Selects how much memory to inject based on remaining token budget."""
    semantic: dict[str, str] = field(default_factory=dict)
    episodic: list[str] = field(default_factory=list)
    working: list[str] = field(default_factory=list)

    TOKEN_BUDGET = 4000  # approximate system prompt budget

    def _estimate_tokens(self, text: str) -> int:
        return len(text) // 4  # rough approximation

    def build_system_prompt(self, base: str = "You are a helpful assistant.") -> str:
        budget = self.TOKEN_BUDGET - self._estimate_tokens(base) - 50
        parts = [base, "\n\n## Memory Context"]

        # Priority 1: working memory (most immediate)
        working_text = "\n".join(f"- {w}" for w in self.working[-3:])
        if working_text:
            cost = self._estimate_tokens(working_text)
            if budget > cost:
                parts.append(f"\n### Current Turn\n{working_text}")
                budget -= cost

        # Priority 2: semantic memory (high importance facts)
        sem_items = list(self.semantic.values())[:10]
        sem_text = "\n".join(f"- {s}" for s in sem_items)
        if sem_text and budget > self._estimate_tokens(sem_text):
            cost = self._estimate_tokens(sem_text)
            if budget > cost:
                parts.append(f"\n### Long-term Facts\n{sem_text}")
                budget -= cost

        # Priority 3: episodic (fill remaining budget)
        for episode in reversed(self.episodic):
            ep_text = f"- {episode}"
            cost = self._estimate_tokens(ep_text)
            if budget < cost:
                break
            parts.append(ep_text)
            budget -= cost

        return "\n".join(parts)

    def store_semantic(self, key: str, value: str):
        self.semantic[key] = value

    def record_episode(self, summary: str):
        self.episodic.append(summary)
        if len(self.episodic) > 50:
            self.episodic.pop(0)

    def set_working(self, item: str):
        self.working.append(item)
        if len(self.working) > 5:
            self.working.pop(0)

mem = TieredMemoryWithBudget()
mem.store_semantic("user",     "User is Carol, a backend engineer.")
mem.store_semantic("stack",    "Carol uses Python + FastAPI + PostgreSQL.")
mem.store_semantic("goal",     "Carol is building a multi-agent orchestration system.")
mem.record_episode("Carol asked about async patterns.")
mem.record_episode("Discussed SQLAlchemy connection pooling.")
mem.set_working("Current task: reviewing retry logic")

system = mem.build_system_prompt()
print(f"System prompt ({mem._estimate_tokens(system)} est. tokens):\n{system[:400]}\n")

resp = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=256,
    system=system,
    messages=[{"role": "user", "content": "What should I use for my DB connection pool?"}],
)
print(f"Agent: {resp.content[0].text}")

# Expected Token Savings: Budget-aware injection prevents context overflow; working memory prioritized
# Environment: pure Python; adjust TOKEN_BUDGET to your model's context window
```

## Option 4: Memory Tier with Compression Pipeline

```python
import anthropic

client = anthropic.Anthropic()

class CompressingMemoryTier:
    """
    Working memory -> compress to episode -> distill episode to semantic fact.
    """
    def __init__(self):
        self.working: list[dict] = []       # raw turns
        self.episodes: list[str] = []       # compressed summaries
        self.semantic: dict[str, str] = {}  # distilled facts
        self.WORKING_MAX = 6
        self.EPISODE_MAX = 10

    def add_turn(self, role: str, content: str):
        self.working.append({"role": role, "content": content})
        if len(self.working) >= self.WORKING_MAX:
            self._compress_working_to_episode()

    def _compress_working_to_episode(self):
        if not self.working:
            return
        conversation = "\n".join(
            f"{t['role'].upper()}: {t['content'][:100]}" for t in self.working
        )
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content":
                f"Summarize in 1 sentence:\n{conversation}"}],
        )
        summary = resp.content[0].text.strip()
        self.episodes.append(summary)
        self.working.clear()
        print(f"  [COMPRESSED working -> episode] {summary[:60]}")

        if len(self.episodes) >= self.EPISODE_MAX:
            self._distill_episodes_to_semantic()

    def _distill_episodes_to_semantic(self):
        if not self.episodes:
            return
        ep_text = "\n".join(f"- {e}" for e in self.episodes)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content":
                f"Extract 3 key persistent facts from these session summaries:\n{ep_text}\n"
                "Format: one fact per line, start with 'FACT:'"}],
        )
        for line in resp.content[0].text.split("\n"):
            if line.startswith("FACT:"):
                fact = line[5:].strip()
                key = f"fact_{abs(hash(fact)) % 9999}"
                self.semantic[key] = fact
                print(f"  [DISTILLED -> semantic] {fact[:60]}")
        self.episodes.clear()

    def get_context(self) -> str:
        parts = []
        if self.semantic:
            parts.append("Facts: " + "; ".join(list(self.semantic.values())[:5]))
        if self.episodes:
            parts.append("Sessions: " + "; ".join(self.episodes[-3:]))
        if self.working:
            recent = self.working[-2:]
            parts.append("Recent: " + " | ".join(
                f"{t['role']}: {t['content'][:50]}" for t in recent
            ))
        return "\n".join(parts)

mem = CompressingMemoryTier()
conversations = [
    ("user", "I prefer async Python for all my projects."),
    ("assistant", "Noted! Async Python is great for IO-bound workloads."),
    ("user", "I always use pytest for testing."),
    ("assistant", "Great choice! Pytest fixtures make testing clean."),
    ("user", "My team uses GitHub for version control."),
    ("assistant", "GitHub is excellent for collaboration."),
    ("user", "What testing framework should I use?"),
]

for role, content in conversations:
    mem.add_turn(role, content)
    if role == "user":
        ctx = mem.get_context()
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            system=f"You are a helpful assistant.\n\n{ctx}" if ctx else "You are a helpful assistant.",
            messages=[{"role": "user", "content": content}],
        )
        print(f"Q: {content[:60]}\nA: {resp.content[0].text[:60]}\n")

# Expected Token Savings: Compression reduces 6-turn history to 1 sentence; semantic distillation is once per 10 episodes
# Environment: two compression steps reduce long-term memory to ~500 tokens total
```

## Option 5: Cross-Session Persistent Semantic Memory with Namespace Isolation

```python
import anthropic
import sqlite3
import time
import hashlib

client = anthropic.Anthropic()
DB = "cross_session_mem.db"

def init_db():
    con = sqlite3.connect(DB)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS semantic (
            namespace TEXT, key TEXT, value TEXT,
            importance REAL, updated_at REAL,
            PRIMARY KEY (namespace, key)
        );
        CREATE TABLE IF NOT EXISTS episodic (
            namespace TEXT, session_id TEXT,
            summary TEXT, ts REAL
        );
        CREATE INDEX IF NOT EXISTS idx_ep ON episodic(namespace, session_id, ts);
    """)
    con.commit(); con.close()

def sem_store(namespace: str, key: str, value: str, importance: float = 0.6):
    con = sqlite3.connect(DB)
    con.execute("INSERT OR REPLACE INTO semantic VALUES (?,?,?,?,?)",
                (namespace, key, value, importance, time.time()))
    con.commit(); con.close()

def sem_get(namespace: str, query: str = "", top_n: int = 5) -> list[str]:
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT key, value FROM semantic WHERE namespace=? ORDER BY importance DESC, updated_at DESC",
        (namespace,)
    ).fetchall()
    con.close()
    if not query:
        return [v for _, v in rows[:top_n]]
    q = query.lower()
    return [v for k, v in rows if any(w in (k+v).lower() for w in q.split())][:top_n]

def ep_record(namespace: str, session_id: str, summary: str):
    con = sqlite3.connect(DB)
    con.execute("INSERT INTO episodic VALUES (?,?,?,?)",
                (namespace, session_id, summary, time.time()))
    # Prune to last 30 per namespace/session
    con.execute("""
        DELETE FROM episodic WHERE namespace=? AND session_id=? AND rowid NOT IN (
            SELECT rowid FROM episodic WHERE namespace=? AND session_id=?
            ORDER BY ts DESC LIMIT 30
        )
    """, (namespace, session_id, namespace, session_id))
    con.commit(); con.close()

def ep_get(namespace: str, session_id: str | None = None, n: int = 5) -> list[str]:
    con = sqlite3.connect(DB)
    if session_id:
        rows = con.execute(
            "SELECT summary FROM episodic WHERE namespace=? AND session_id=? ORDER BY ts DESC LIMIT ?",
            (namespace, session_id, n)
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT summary FROM episodic WHERE namespace=? ORDER BY ts DESC LIMIT ?",
            (namespace, n)
        ).fetchall()
    con.close()
    return [r[0] for r in reversed(rows)]

def respond(namespace: str, session_id: str, user_input: str) -> str:
    facts = sem_get(namespace, user_input, top_n=3)
    episodes = ep_get(namespace, session_id, n=3)
    ctx_parts = []
    if facts:    ctx_parts.append("Facts: " + "; ".join(facts))
    if episodes: ctx_parts.append("Recent: " + "; ".join(episodes))

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=("You are a helpful assistant.\n\n" + "\n".join(ctx_parts)) if ctx_parts
               else "You are a helpful assistant.",
        messages=[{"role": "user", "content": user_input}],
    )
    result = resp.content[0].text
    ep_record(namespace, session_id, f"Q:{user_input[:40]} A:{result[:40]}")
    return result

init_db()
# User "alice" — namespace isolates her memory from other users
sem_store("user:alice", "pref_lang", "Alice uses Python 3.12.", importance=0.9)
sem_store("user:alice", "pref_db",   "Alice prefers PostgreSQL.", importance=0.8)

for session in ["sess_1", "sess_2"]:
    print(f"\n=== Session {session} ===")
    for q in ["What language do I use?", "What database do I prefer?"]:
        print(f"Q: {q}\nA: {respond('user:alice', session, q)}\n")

# Expected Token Savings: Namespace isolation prevents cross-user leakage; only user-specific facts loaded
# Environment: SQLite; swap namespace=f"user:{user_id}" for multi-tenant isolation
```

## Option 6: Reactive Memory — Tier Selection Based on Query Type

```python
import anthropic
import re
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class ReactiveMemory:
    """Selects which tier(s) to query based on the nature of the user input."""
    semantic: dict[str, str] = field(default_factory=dict)
    episodic: list[dict] = field(default_factory=list)  # {"ts": float, "text": str}
    working: list[str] = field(default_factory=list)    # current turn buffer

    def classify_query(self, query: str) -> list[str]:
        """Return which tiers are relevant for this query type."""
        q = query.lower()
        tiers = []
        # Working always consulted if non-empty
        if self.working:
            tiers.append("working")
        # Episodic for recency/history questions
        if re.search(r"\b(last|previous|earlier|before|ago|yesterday|recently)\b", q):
            tiers.append("episodic")
        # Semantic for identity/preference/factual questions
        if re.search(r"\b(my|i am|i prefer|i use|what is|who am|name|always|never)\b", q):
            tiers.append("semantic")
        # Default: use all
        if not tiers:
            tiers = ["semantic", "episodic"]
        return tiers

    def retrieve(self, query: str) -> str:
        tiers = self.classify_query(query)
        print(f"  [TIERS] selected: {tiers}")
        parts = []
        if "working" in tiers:
            parts.append("Now: " + "; ".join(self.working[-3:]))
        if "semantic" in tiers:
            q = query.lower()
            hits = [v for k, v in self.semantic.items()
                    if any(w in (k+v).lower() for w in q.split())][:4]
            if hits: parts.append("Facts: " + "; ".join(hits))
        if "episodic" in tiers:
            recent = [e["text"] for e in self.episodic[-5:]]
            if recent: parts.append("History: " + "; ".join(recent))
        return "\n".join(parts)

    def record(self, user: str, assistant: str):
        import time
        self.episodic.append({"ts": time.time(), "text": f"U:{user[:40]} A:{assistant[:40]}"})
        self.working = [user[:40]]  # set working to latest user input

mem = ReactiveMemory()
mem.semantic["name"]   = "User is Dave."
mem.semantic["stack"]  = "Dave uses Go and Kubernetes."
mem.semantic["prefer"] = "Dave prefers functional programming patterns."

# Simulate session
turns = [
    "What is my name?",
    "What was my last question?",
    "What language do I use?",
    "What do you recommend for container orchestration?",
]

for q in turns:
    ctx = mem.retrieve(q)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        system=f"You are a helpful assistant.\n\nContext:\n{ctx}" if ctx
               else "You are a helpful assistant.",
        messages=[{"role": "user", "content": q}],
    )
    answer = resp.content[0].text
    mem.record(q, answer)
    print(f"Q: {q}\nA: {answer.strip()[:80]}\n")

# Expected Token Savings: Only relevant tiers queried per turn; episodic skipped on factual queries
# Environment: pure Python; classify_query regex tunable to your domain vocabulary
```

## Comparison

| Option | Tiers | Storage | Compression |
|--------|-------|---------|-------------|
| 1 — Basic Three-Tier | Working / Episodic / Semantic | In-memory | Manual clear |
| 2 — SQLite Tiered | All three | SQLite persistent | Auto-promote episodes |
| 3 — Budget-Aware | All three | In-memory | Token-budget gating |
| 4 — Compression Pipeline | Working → Episode → Semantic | In-memory | LLM compression |
| 5 — Cross-Session Namespaced | Episodic + Semantic | SQLite | Namespace isolation |
| 6 — Reactive Selection | All three | In-memory | Query-type-driven |
