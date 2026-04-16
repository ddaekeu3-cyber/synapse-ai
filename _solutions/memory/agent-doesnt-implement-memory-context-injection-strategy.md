---
layout: solution
title: "Agent Doesn't Implement Memory Context Injection Strategy"
category: memory
description: "Strategically select and inject the most relevant memories into the context window instead of dumping all stored facts, preserving token budget and relevance."
tags: [memory, context, injection, relevance, retrieval, token-budget]
---

# Agent Doesn't Implement Memory Context Injection Strategy

Agents with persistent memory often concatenate all stored facts into every prompt, wasting tokens and drowning relevant memories in noise. A proper injection strategy retrieves only the memories that are relevant to the current turn, ranks them by recency and importance, and injects them within a configurable token budget.

## Option 1: Keyword-Match Injection with Token Budget

```python
import anthropic

client = anthropic.Anthropic()

MEMORY_STORE: list[dict] = [
    {"id": 1, "text": "User prefers Python over JavaScript.", "tags": ["python", "language"]},
    {"id": 2, "text": "User is building a Telegram bot.", "tags": ["telegram", "bot"]},
    {"id": 3, "text": "User's API key is stored in .env file.", "tags": ["api", "env", "config"]},
    {"id": 4, "text": "User dislikes verbose explanations.", "tags": ["style", "brevity"]},
    {"id": 5, "text": "User's project is called SynapseAI.", "tags": ["project", "name"]},
]

TOKEN_BUDGET = 200  # max tokens for injected memories


def keyword_score(memory: dict, query: str) -> int:
    query_words = set(query.lower().split())
    tag_hits = sum(1 for tag in memory["tags"] if tag in query_words)
    text_hits = sum(1 for word in query_words if word in memory["text"].lower())
    return tag_hits * 2 + text_hits


def inject_memories(query: str, budget: int = TOKEN_BUDGET) -> str:
    scored = sorted(MEMORY_STORE, key=lambda m: keyword_score(m, query), reverse=True)
    injected = []
    used_tokens = 0
    for m in scored:
        # Approximate: 4 chars ≈ 1 token
        approx_tokens = len(m["text"]) // 4
        if used_tokens + approx_tokens > budget:
            break
        injected.append(m["text"])
        used_tokens += approx_tokens
    return "\n".join(f"- {t}" for t in injected) if injected else ""


def run_agent(user_message: str) -> str:
    memory_block = inject_memories(user_message)
    system = "You are a helpful assistant."
    if memory_block:
        system += f"\n\nRelevant context about this user:\n{memory_block}"

    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return r.content[0].text


if __name__ == "__main__":
    print(run_agent("How should I structure my Telegram bot project?"))

# Expected Token Savings: Injects 1-3 relevant memories instead of all 5; saves ~60% context
# Environment: Python 3.9+; replace MEMORY_STORE with your persistent memory backend
```

## Option 2: Recency + Importance Ranked Injection

```python
import time
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()


@dataclass
class Memory:
    id: int
    text: str
    importance: float       # 0.0–1.0, set at write time
    created_at: float = field(default_factory=time.time)
    access_count: int = 0


MEMORIES: list[Memory] = [
    Memory(1, "User prefers async Python patterns.", importance=0.9, created_at=time.time() - 86400),
    Memory(2, "User's database is PostgreSQL.", importance=0.7, created_at=time.time() - 3600),
    Memory(3, "User asked about retry logic yesterday.", importance=0.5, created_at=time.time() - 7200),
    Memory(4, "User's agent processes 10k messages/day.", importance=0.8, created_at=time.time() - 600),
    Memory(5, "User is on the Pro Anthropic plan.", importance=0.6, created_at=time.time() - 172800),
]

MAX_INJECT = 3  # max memories to inject per turn
RECENCY_WEIGHT = 0.4
IMPORTANCE_WEIGHT = 0.6


def rank_memories(memories: list[Memory]) -> list[Memory]:
    now = time.time()
    max_age = max((now - m.created_at) for m in memories) or 1.0

    def score(m: Memory) -> float:
        recency = 1.0 - (now - m.created_at) / max_age
        return RECENCY_WEIGHT * recency + IMPORTANCE_WEIGHT * m.importance

    return sorted(memories, key=score, reverse=True)


def build_memory_block(limit: int = MAX_INJECT) -> str:
    top = rank_memories(MEMORIES)[:limit]
    for m in top:
        m.access_count += 1
    return "\n".join(f"- {m.text}" for m in top)


def run_agent(user_input: str) -> str:
    memory_block = build_memory_block()
    system = f"You are a helpful assistant.\n\nUser context:\n{memory_block}"
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_input}],
    )
    return r.content[0].text


if __name__ == "__main__":
    print(run_agent("What's the best way to handle high-volume message processing?"))

# Expected Token Savings: Top-3 injection saves ~70% vs injecting all memories
# Environment: Python 3.9+; tune RECENCY_WEIGHT and IMPORTANCE_WEIGHT for your use case
```

## Option 3: SQLite-Backed Memory with Relevance Search

```python
import sqlite3
import time
import anthropic

DB_PATH = "agent_memory.db"
client = anthropic.Anthropic()
MAX_INJECT_TOKENS = 300


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memories USING fts5(
            text, tags, importance UNINDEXED, created_at UNINDEXED
        )
    """)
    conn.commit()
    return conn


def store_memory(conn: sqlite3.Connection, text: str, tags: str, importance: float = 0.5) -> None:
    conn.execute(
        "INSERT INTO memories VALUES (?,?,?,?)",
        (text, tags, importance, time.time()),
    )
    conn.commit()


def retrieve_relevant(conn: sqlite3.Connection, query: str, limit: int = 5) -> list[dict]:
    # FTS5 full-text search
    try:
        rows = conn.execute(
            "SELECT text, importance, created_at FROM memories WHERE memories MATCH ? ORDER BY rank LIMIT ?",
            (query, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        # Fallback: return most recent
        rows = conn.execute(
            "SELECT text, importance, created_at FROM memories ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [{"text": r[0], "importance": r[1], "created_at": r[2]} for r in rows]


def inject_within_budget(memories: list[dict], token_budget: int) -> str:
    lines = []
    used = 0
    for m in sorted(memories, key=lambda x: x["importance"], reverse=True):
        approx = len(m["text"]) // 4
        if used + approx > token_budget:
            break
        lines.append(f"- {m['text']}")
        used += approx
    return "\n".join(lines)


def run_agent(user_input: str) -> str:
    conn = init_db()

    # Seed memories on first run
    if conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0:
        seed = [
            ("User prefers concise code examples.", "code style python", 0.8),
            ("User is building a multi-agent orchestration system.", "agent architecture", 0.9),
            ("User uses SQLite for lightweight persistence.", "sqlite database storage", 0.7),
            ("User's timezone is UTC+9.", "timezone schedule", 0.4),
        ]
        for text, tags, imp in seed:
            store_memory(conn, text, tags, imp)

    relevant = retrieve_relevant(conn, user_input)
    memory_block = inject_within_budget(relevant, MAX_INJECT_TOKENS)
    conn.close()

    system = "You are a helpful assistant."
    if memory_block:
        system += f"\n\nRelevant user context:\n{memory_block}"

    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_input}],
    )
    return r.content[0].text


if __name__ == "__main__":
    print(run_agent("How should I store task state in my agent system?"))

# Expected Token Savings: FTS5 retrieves 3-5 relevant memories from thousands; scales to large stores
# Environment: Python 3.9+, SQLite3 with FTS5 (default in Python's sqlite3); persist DB across sessions
```

## Option 4: Tiered Injection — Core + Contextual Layers

```python
import time
import anthropic
from dataclasses import dataclass

client = anthropic.Anthropic()


@dataclass
class Memory:
    text: str
    tier: str   # "core" (always inject) | "contextual" (inject if relevant)
    tags: list[str]
    importance: float = 0.5


MEMORIES: list[Memory] = [
    # Core memories — always injected (user identity, critical preferences)
    Memory("User's name is Alex.", tier="core", tags=[], importance=1.0),
    Memory("User communicates in English only.", tier="core", tags=[], importance=1.0),
    # Contextual memories — injected only when relevant
    Memory("User's project uses FastAPI.", tier="contextual", tags=["api", "fastapi", "web", "http"]),
    Memory("User's Anthropic model budget is $50/month.", tier="contextual", tags=["cost", "budget", "token"]),
    Memory("User prefers type annotations in Python.", tier="contextual", tags=["python", "type", "annotation"]),
    Memory("User's agent runs on a 2-core VM.", tier="contextual", tags=["performance", "cpu", "server", "vm"]),
    Memory("User stores embeddings in Chroma.", tier="contextual", tags=["embedding", "vector", "chroma", "search"]),
]

CORE_TOKEN_BUDGET = 80
CONTEXTUAL_TOKEN_BUDGET = 150


def is_relevant(memory: Memory, query: str) -> bool:
    query_lower = query.lower()
    return any(tag in query_lower for tag in memory.tags)


def build_tiered_context(query: str) -> str:
    core_memories = [m for m in MEMORIES if m.tier == "core"]
    contextual_candidates = [m for m in MEMORIES if m.tier == "contextual" and is_relevant(m, query)]

    # Sort contextual by importance
    contextual_candidates.sort(key=lambda m: m.importance, reverse=True)

    def budget_select(memories: list[Memory], budget: int) -> list[str]:
        selected = []
        used = 0
        for m in memories:
            cost = len(m.text) // 4
            if used + cost > budget:
                break
            selected.append(f"- {m.text}")
            used += cost
        return selected

    core_lines = budget_select(core_memories, CORE_TOKEN_BUDGET)
    ctx_lines = budget_select(contextual_candidates, CONTEXTUAL_TOKEN_BUDGET)

    parts = []
    if core_lines:
        parts.append("Always known:\n" + "\n".join(core_lines))
    if ctx_lines:
        parts.append("Relevant context:\n" + "\n".join(ctx_lines))
    return "\n\n".join(parts)


def run_agent(user_input: str) -> str:
    context = build_tiered_context(user_input)
    system = "You are a helpful assistant."
    if context:
        system += f"\n\n{context}"

    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_input}],
    )
    return r.content[0].text


if __name__ == "__main__":
    print(run_agent("How can I reduce my token costs with caching?"))

# Expected Token Savings: Core layer uses fixed ~80 tokens; contextual adds 0-150 based on query
# Environment: Python 3.9+; extend tiers (e.g., "session" for within-session facts)
```

## Option 5: LLM-Scored Relevance Filter Before Injection

```python
import json
import anthropic

client = anthropic.Anthropic()

MEMORIES = [
    "User is building a real-time chat application.",
    "User prefers Redis for session storage.",
    "User's server has 8GB RAM.",
    "User works in the fintech industry.",
    "User's agent processes compliance documents.",
    "User's team uses GitHub Actions for CI.",
    "User dislikes overly abstract code patterns.",
]

RELEVANCE_PROMPT = """Given the user's current question and a list of stored memories,
return a JSON array of the indices (0-based) of the memories that are directly relevant.
Return at most 3 indices. If none are relevant, return [].

Question: {question}

Memories:
{memories}

Return only the JSON array, no explanation."""


def select_relevant_memories(query: str, memories: list[str]) -> list[str]:
    if not memories:
        return []

    mem_block = "\n".join(f"{i}. {m}" for i, m in enumerate(memories))
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": RELEVANCE_PROMPT.format(
            question=query, memories=mem_block
        )}],
    )
    try:
        indices = json.loads(r.content[0].text.strip())
        return [memories[i] for i in indices if 0 <= i < len(memories)]
    except (json.JSONDecodeError, IndexError):
        return memories[:3]  # fallback


def run_agent(user_input: str) -> str:
    selected = select_relevant_memories(user_input, MEMORIES)
    system = "You are a helpful assistant."
    if selected:
        block = "\n".join(f"- {m}" for m in selected)
        system += f"\n\nRelevant user context:\n{block}"

    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_input}],
    )
    return r.content[0].text


if __name__ == "__main__":
    print(run_agent("Should I use Redis Streams or pub/sub for my chat app?"))

# Expected Token Savings: LLM filter reduces injection to ≤3 memories; costs ~60 haiku tokens
# Environment: Python 3.9+; use when keyword matching is insufficient for complex memory topics
```

## Option 6: Async Injection Pipeline with Sliding Window + Dedup

```python
import asyncio
import time
import hashlib
import anthropic
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()


@dataclass
class Memory:
    text: str
    created_at: float = field(default_factory=time.time)
    importance: float = 0.5
    _hash: str = ""

    def __post_init__(self) -> None:
        self._hash = hashlib.md5(self.text.encode()).hexdigest()[:8]


class SlidingWindowMemoryStore:
    def __init__(self, window_size: int = 20) -> None:
        self._store: list[Memory] = []
        self._window_size = window_size
        self._seen_hashes: set[str] = set()

    def add(self, text: str, importance: float = 0.5) -> None:
        m = Memory(text=text, importance=importance)
        if m._hash in self._seen_hashes:
            return  # dedup
        self._store.append(m)
        self._seen_hashes.add(m._hash)
        # Evict oldest low-importance memories beyond window
        if len(self._store) > self._window_size:
            self._store.sort(key=lambda x: x.importance * 0.3 + (1 - (time.time() - x.created_at) / 86400) * 0.7, reverse=True)
            evicted = self._store[self._window_size:]
            self._store = self._store[:self._window_size]
            for e in evicted:
                self._seen_hashes.discard(e._hash)

    def get_top(self, n: int = 4) -> list[Memory]:
        now = time.time()
        max_age = max((now - m.created_at for m in self._store), default=1.0)

        def score(m: Memory) -> float:
            recency = 1.0 - (now - m.created_at) / max_age
            return 0.5 * m.importance + 0.5 * recency

        return sorted(self._store, key=score, reverse=True)[:n]


STORE = SlidingWindowMemoryStore(window_size=20)


async def run_agent(user_input: str) -> str:
    top = STORE.get_top(n=4)
    memory_block = "\n".join(f"- {m.text}" for m in top)

    system = "You are a helpful assistant."
    if memory_block:
        system += f"\n\nTop user context:\n{memory_block}"

    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_input}],
    )

    # Store the interaction as a memory for future turns
    summary = f"User asked: {user_input[:80]}"
    STORE.add(summary, importance=0.4)
    return r.content[0].text


async def main() -> None:
    STORE.add("User is building an async Python agent.", importance=0.9)
    STORE.add("User prefers SQLite for lightweight persistence.", importance=0.7)
    STORE.add("User's agent handles 500 requests/hour.", importance=0.8)
    STORE.add("User is building an async Python agent.", importance=0.9)  # duplicate — deduped

    result = await run_agent("How can I make my agent handle more requests efficiently?")
    print(result)
    print(f"\n[STORE] {len(STORE._store)} memories stored (deduped)")


asyncio.run(main())

# Expected Token Savings: Sliding window + dedup prevents unbounded context growth
# Environment: Python 3.11+, asyncio; window_size controls memory footprint
```

## Comparison

| Option | Retrieval Method | Token Budget | Dedup | Persistence | Best For |
|--------|-----------------|-------------|-------|-------------|----------|
| 1. Keyword Match | Tag/word overlap | Hard cap | No | In-memory | Simple rule-based retrieval |
| 2. Recency + Importance | Weighted score | Top-N limit | No | In-memory | Balanced relevance |
| 3. SQLite FTS5 | Full-text search | Token budget | No | SQLite | Large memory stores |
| 4. Tiered Layers | Tag match + tier | Per-tier budget | No | In-memory | Core vs. contextual split |
| 5. LLM Filter | Haiku relevance judge | Top-3 | No | In-memory | Semantic relevance |
| 6. Sliding Window | Score + eviction | Window size | Yes | In-memory | Async, long-running agents |
