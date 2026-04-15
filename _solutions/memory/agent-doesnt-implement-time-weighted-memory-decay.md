---
layout: solution
title: "Agent Doesn't Implement Time-Weighted Memory Decay"
category: memory
description: "Agents treat all stored memories equally regardless of age. Without time-weighted decay, old irrelevant memories compete with recent ones, context becomes polluted with stale data, and retrieval quality degrades over time."
tags: [memory, decay, time-weighted, staleness, retrieval, long-term-memory]
---

# Agent Doesn't Implement Time-Weighted Memory Decay

## Problem

Memory systems that store facts indefinitely give equal weight to information from months ago and information from five minutes ago. A user's preferences from six months ago may conflict with current ones. Old task context clutters retrieval results. Without temporal weighting, memory retrieval returns stale data as confidently as fresh data — degrading response quality over time.

## Why This Happens

Simple key-value or vector stores don't track time as a retrieval dimension. Teams add timestamps to memories but never use them during retrieval. The decay logic requires periodic maintenance that is easy to defer, and without visible failures, the problem goes unnoticed until the memory store is full of outdated entries.

## Solutions

### Option 1: Exponential Decay Score — Weight memories by recency during retrieval

```python
import anthropic
import math
import time
from dataclasses import dataclass, field

DECAY_HALF_LIFE_DAYS = 7.0   # Memory score halves every 7 days


def decay_weight(created_at: float, half_life_days: float = DECAY_HALF_LIFE_DAYS) -> float:
    """Exponential decay: returns 0-1 weight based on age."""
    age_days = (time.time() - created_at) / 86400
    return math.exp(-math.log(2) * age_days / half_life_days)


@dataclass
class Memory:
    key: str
    content: str
    created_at: float = field(default_factory=time.time)
    relevance_score: float = 1.0   # From embedding similarity or explicit rating

    def effective_score(self, half_life_days: float = DECAY_HALF_LIFE_DAYS) -> float:
        """Combined score: relevance * time decay."""
        return self.relevance_score * decay_weight(self.created_at, half_life_days)

    def age_days(self) -> float:
        return (time.time() - self.created_at) / 86400


class DecayingMemoryStore:
    def __init__(self, half_life_days: float = DECAY_HALF_LIFE_DAYS, max_memories: int = 200):
        self.memories: list[Memory] = []
        self.half_life_days = half_life_days
        self.max_memories = max_memories

    def add(self, key: str, content: str, relevance: float = 1.0) -> None:
        # Replace if key exists
        self.memories = [m for m in self.memories if m.key != key]
        self.memories.append(Memory(key=key, content=content, relevance_score=relevance))
        self._prune_if_needed()

    def retrieve(self, top_k: int = 5, min_score: float = 0.1) -> list[Memory]:
        """Return top-k memories by effective (decayed) score."""
        scored = [m for m in self.memories if m.effective_score(self.half_life_days) >= min_score]
        return sorted(scored, key=lambda m: m.effective_score(self.half_life_days), reverse=True)[:top_k]

    def _prune_if_needed(self) -> None:
        """Remove lowest-scored memories when over capacity."""
        if len(self.memories) > self.max_memories:
            self.memories.sort(key=lambda m: m.effective_score(self.half_life_days), reverse=True)
            removed = self.memories[self.max_memories:]
            self.memories = self.memories[:self.max_memories]
            if removed:
                print(f"[DECAY] Pruned {len(removed)} low-scoring memories")

    def debug_scores(self) -> list[dict]:
        return [
            {
                "key": m.key,
                "age_days": round(m.age_days(), 2),
                "relevance": m.relevance_score,
                "decay": round(decay_weight(m.created_at, self.half_life_days), 3),
                "effective": round(m.effective_score(self.half_life_days), 3),
            }
            for m in sorted(self.memories, key=lambda m: m.effective_score(self.half_life_days), reverse=True)
        ]


class DecayAwareAgent:
    def __init__(self):
        self.client = anthropic.Anthropic()
        self.memory = DecayingMemoryStore(half_life_days=7.0)

    def remember(self, key: str, content: str, relevance: float = 1.0) -> None:
        self.memory.add(key, content, relevance)
        print(f"[MEMORY] Stored '{key}'")

    def chat(self, user_message: str) -> str:
        relevant = self.memory.retrieve(top_k=5)

        context = ""
        if relevant:
            memory_lines = [
                f"- [{m.key}] (score: {m.effective_score():.2f}, age: {m.age_days():.1f}d): {m.content}"
                for m in relevant
            ]
            context = "Relevant memories (sorted by recency-weighted relevance):\n" + "\n".join(memory_lines)

        system = "You are a helpful assistant with access to time-weighted memories."
        if context:
            system += f"\n\n{context}"

        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=system,
            messages=[{"role": "user", "content": user_message}]
        )
        return response.content[0].text


# Usage
agent = DecayAwareAgent()

# Simulate memories at different ages (adjust created_at for demo)
old_memory = Memory("old_pref", "User preferred dark mode", created_at=time.time() - 30 * 86400)
recent_memory = Memory("recent_pref", "User switched to light mode yesterday", created_at=time.time() - 86400)
agent.memory.memories.extend([old_memory, recent_memory])

print("Memory scores:")
for item in agent.memory.debug_scores():
    print(f"  {item}")

reply = agent.chat("What display mode should I use?")
print(f"\nAgent: {reply}")

# Expected Token Savings: Fresh context means fewer tokens wasted on stale contradictory memories
# Environment: Personal assistants, long-running bots, any agent with persistent user memory
```

### Option 2: Tiered Decay — Different decay rates for different memory categories

```python
import anthropic
import math
import time
from dataclasses import dataclass, field
from enum import Enum

class MemoryTier(Enum):
    PERMANENT = "permanent"   # No decay: user name, account ID, stable facts
    SLOW = "slow"             # Decays over months: preferences, work context
    NORMAL = "normal"         # Decays over weeks: recent topics, project details
    FAST = "fast"             # Decays over days: session context, current tasks
    EPHEMERAL = "ephemeral"   # Decays in hours: temporary notes, draft states

TIER_HALF_LIFE_DAYS = {
    MemoryTier.PERMANENT: float("inf"),
    MemoryTier.SLOW: 60.0,
    MemoryTier.NORMAL: 14.0,
    MemoryTier.FAST: 3.0,
    MemoryTier.EPHEMERAL: 0.25,  # 6 hours
}


@dataclass
class TieredMemory:
    key: str
    content: str
    tier: MemoryTier
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    access_count: int = 0

    def decay_score(self) -> float:
        half_life = TIER_HALF_LIFE_DAYS[self.tier]
        if math.isinf(half_life):
            return 1.0
        age = (time.time() - self.created_at) / 86400
        base_decay = math.exp(-math.log(2) * age / half_life)
        # Boost for frequently accessed memories
        access_boost = min(0.3, self.access_count * 0.02)
        return min(1.0, base_decay + access_boost)

    def is_expired(self, threshold: float = 0.05) -> bool:
        return self.decay_score() < threshold

    def record_access(self) -> None:
        self.last_accessed = time.time()
        self.access_count += 1


class TieredMemoryStore:
    def __init__(self):
        self._memories: dict[str, TieredMemory] = {}

    def store(self, key: str, content: str, tier: MemoryTier) -> None:
        self._memories[key] = TieredMemory(key=key, content=content, tier=tier)

    def retrieve(self, tier_filter: list[MemoryTier] | None = None, top_k: int = 10) -> list[TieredMemory]:
        memories = list(self._memories.values())

        # Filter by tier if specified
        if tier_filter:
            memories = [m for m in memories if m.tier in tier_filter]

        # Remove expired memories
        memories = [m for m in memories if not m.is_expired()]

        # Sort by decay score
        memories.sort(key=lambda m: m.decay_score(), reverse=True)

        # Record access
        result = memories[:top_k]
        for m in result:
            m.record_access()

        return result

    def cleanup_expired(self) -> int:
        expired = [k for k, m in self._memories.items() if m.is_expired()]
        for k in expired:
            del self._memories[k]
        return len(expired)

    def summary(self) -> dict:
        by_tier = {}
        for m in self._memories.values():
            tier_name = m.tier.value
            if tier_name not in by_tier:
                by_tier[tier_name] = {"count": 0, "avg_score": 0.0}
            by_tier[tier_name]["count"] += 1
            by_tier[tier_name]["avg_score"] += m.decay_score()
        for tier_data in by_tier.values():
            if tier_data["count"]:
                tier_data["avg_score"] = round(tier_data["avg_score"] / tier_data["count"], 2)
        return by_tier


class TieredMemoryAgent:
    def __init__(self):
        self.client = anthropic.Anthropic()
        self.store = TieredMemoryStore()

    def chat(self, user_message: str) -> str:
        # Retrieve memories prioritizing recent context but including stable facts
        context_memories = self.store.retrieve(
            tier_filter=[MemoryTier.PERMANENT, MemoryTier.SLOW, MemoryTier.NORMAL, MemoryTier.FAST],
            top_k=8
        )

        memory_text = "\n".join(
            f"[{m.tier.value.upper()}] {m.content} (score: {m.decay_score():.2f})"
            for m in context_memories
        ) if context_memories else "No memories available."

        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=f"You are a personalized assistant.\n\nMemory context:\n{memory_text}",
            messages=[{"role": "user", "content": user_message}]
        )
        return response.content[0].text


# Usage
agent = TieredMemoryAgent()
agent.store.store("user_name", "Alice", MemoryTier.PERMANENT)
agent.store.store("preferred_language", "Python", MemoryTier.SLOW)
agent.store.store("current_project", "Building a REST API", MemoryTier.NORMAL)
agent.store.store("last_error", "TypeError on line 42", MemoryTier.FAST)
agent.store.store("draft_note", "Check this later", MemoryTier.EPHEMERAL)

print("Store summary:", agent.store.summary())
reply = agent.chat("What was I working on recently?")
print(reply)

# Expected Token Savings: Ephemeral/fast memories prune automatically; permanent facts always present
# Environment: Personal assistants, coding agents, long-term user-facing chatbots
```

### Option 3: Access-Reinforced Decay — Memories reset decay clock on each access

```python
import anthropic
import math
import time
from dataclasses import dataclass, field

INITIAL_HALF_LIFE_DAYS = 7.0
ACCESS_REINFORCEMENT_DAYS = 3.0  # Each access adds this many days to effective age


@dataclass
class ReinforcedMemory:
    key: str
    content: str
    created_at: float = field(default_factory=time.time)
    reinforcement_total: float = 0.0  # Total days added via access reinforcement
    access_count: int = 0

    def access(self) -> None:
        """Record an access event, reinforcing this memory."""
        self.access_count += 1
        self.reinforcement_total += ACCESS_REINFORCEMENT_DAYS

    def effective_age_days(self) -> float:
        raw_age = (time.time() - self.created_at) / 86400
        return max(0.0, raw_age - self.reinforcement_total)

    def score(self) -> float:
        eff_age = self.effective_age_days()
        return math.exp(-math.log(2) * eff_age / INITIAL_HALF_LIFE_DAYS)

    def is_alive(self) -> bool:
        return self.score() >= 0.05


class ReinforcedMemoryStore:
    def __init__(self, max_size: int = 500):
        self._store: dict[str, ReinforcedMemory] = {}
        self.max_size = max_size

    def remember(self, key: str, content: str) -> None:
        if key in self._store:
            # Re-remember: update content and reinforce
            self._store[key].content = content
            self._store[key].access()
        else:
            self._store[key] = ReinforcedMemory(key=key, content=content)

        if len(self._store) > self.max_size:
            self._evict()

    def recall(self, keys: list[str] | None = None, top_k: int = 10) -> list[ReinforcedMemory]:
        if keys:
            memories = [self._store[k] for k in keys if k in self._store]
        else:
            memories = list(self._store.values())

        alive = [m for m in memories if m.is_alive()]
        alive.sort(key=lambda m: m.score(), reverse=True)

        result = alive[:top_k]
        for m in result:
            m.access()  # Reinforce accessed memories

        return result

    def _evict(self) -> None:
        """Remove lowest-scoring memories."""
        sorted_memories = sorted(self._store.items(), key=lambda kv: kv[1].score())
        evict_count = len(self._store) - self.max_size
        for key, _ in sorted_memories[:evict_count]:
            del self._store[key]

    def stats(self) -> dict:
        alive = [m for m in self._store.values() if m.is_alive()]
        return {
            "total": len(self._store),
            "alive": len(alive),
            "avg_score": round(sum(m.score() for m in alive) / len(alive), 3) if alive else 0,
            "avg_accesses": round(sum(m.access_count for m in alive) / len(alive), 1) if alive else 0,
        }


class ReinforcedMemoryAgent:
    def __init__(self):
        self.client = anthropic.Anthropic()
        self.store = ReinforcedMemoryStore()

    def observe(self, key: str, fact: str) -> None:
        self.store.remember(key, fact)

    def chat(self, user_message: str) -> str:
        memories = self.store.recall(top_k=6)
        context = "\n".join(f"- {m.key}: {m.content} (score={m.score():.2f}, accesses={m.access_count})"
                             for m in memories) if memories else "No memories."

        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=f"You are a helpful assistant.\n\nMemories:\n{context}",
            messages=[{"role": "user", "content": user_message}]
        )

        # Store the conversation as a memory (will reinforce if discussed again)
        self.store.remember(f"convo_{int(time.time())}", f"User asked: {user_message[:100]}")
        return response.content[0].text


# Usage
agent = ReinforcedMemoryAgent()
agent.observe("user_goal", "Build a machine learning pipeline")
agent.observe("tech_stack", "Python, scikit-learn, pandas")
agent.observe("deadline", "End of Q2 2026")

# Simulate repeated references to tech stack (reinforces it)
agent.store.remember("tech_stack", "Python, scikit-learn, pandas")
agent.store.remember("tech_stack", "Python, scikit-learn, pandas")

print("Stats:", agent.store.stats())
reply = agent.chat("Remind me what I'm working on.")
print(reply)

# Expected Token Savings: Frequently-used memories persist longer; stale-but-accessed stays fresh
# Environment: Tutoring agents, coding assistants, any agent benefiting from usage-based retention
```

### Option 4: SQLite Decay Store — Persistent time-weighted memory with scheduled cleanup

```python
import anthropic
import math
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

DB_PATH = Path("/tmp/memory_decay.db")
HALF_LIFE_DAYS = 10.0
CLEANUP_INTERVAL = 3600  # Run cleanup hourly


class SQLiteDecayStore:
    def __init__(self, db_path: Path = DB_PATH, half_life_days: float = HALF_LIFE_DAYS):
        self.db = db_path
        self.half_life = half_life_days
        self._last_cleanup = 0.0
        self._init()

    def _init(self) -> None:
        with sqlite3.connect(self.db) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    key TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    relevance REAL DEFAULT 1.0,
                    created_at REAL NOT NULL,
                    last_accessed REAL NOT NULL,
                    access_count INTEGER DEFAULT 0,
                    PRIMARY KEY (key, agent_id)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_agent ON memories(agent_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_created ON memories(created_at)")

    def _decay_score_sql(self) -> str:
        """SQL expression for exponential decay score."""
        half_life_seconds = self.half_life * 86400
        return f"(relevance * exp(-0.693 * (strftime('%s','now') - created_at) / {half_life_seconds}))"

    def store(self, agent_id: str, key: str, content: str, relevance: float = 1.0) -> None:
        now = time.time()
        with sqlite3.connect(self.db) as conn:
            conn.execute("""
                INSERT INTO memories(key, agent_id, content, relevance, created_at, last_accessed, access_count)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(key, agent_id) DO UPDATE SET
                    content=excluded.content,
                    relevance=excluded.relevance,
                    last_accessed=excluded.last_accessed,
                    access_count=access_count+1
            """, (key, agent_id, content, relevance, now, now))

    def retrieve(self, agent_id: str, top_k: int = 8, min_score: float = 0.05) -> list[dict]:
        decay_expr = self._decay_score_sql()
        with sqlite3.connect(self.db) as conn:
            # Update last_accessed and access_count for retrieved rows
            rows = conn.execute(f"""
                SELECT key, content, relevance, created_at,
                       {decay_expr} as score,
                       (strftime('%s','now') - created_at) / 86400.0 as age_days
                FROM memories
                WHERE agent_id = ? AND {decay_expr} >= ?
                ORDER BY {decay_expr} DESC
                LIMIT ?
            """, (agent_id, min_score, top_k)).fetchall()

            # Bump access count
            if rows:
                keys = [r[0] for r in rows]
                conn.executemany(
                    "UPDATE memories SET last_accessed=?, access_count=access_count+1 WHERE key=? AND agent_id=?",
                    [(time.time(), k, agent_id) for k in keys]
                )

        return [
            {"key": r[0], "content": r[1], "relevance": r[2],
             "score": round(r[4], 3), "age_days": round(r[5], 1)}
            for r in rows
        ]

    def cleanup_expired(self, agent_id: str | None = None, min_score: float = 0.05) -> int:
        now = time.time()
        if now - self._last_cleanup < CLEANUP_INTERVAL:
            return 0  # Skip if cleaned recently
        self._last_cleanup = now

        decay_expr = self._decay_score_sql()
        where = f"WHERE {decay_expr} < ?"
        params = [min_score]
        if agent_id:
            where += " AND agent_id = ?"
            params.append(agent_id)

        with sqlite3.connect(self.db) as conn:
            result = conn.execute(f"DELETE FROM memories {where}", params)
            deleted = result.rowcount
        if deleted:
            print(f"[DECAY] Cleaned up {deleted} expired memories")
        return deleted

    def agent_stats(self, agent_id: str) -> dict:
        decay_expr = self._decay_score_sql()
        with sqlite3.connect(self.db) as conn:
            row = conn.execute(f"""
                SELECT COUNT(*), AVG({decay_expr}), AVG(access_count)
                FROM memories WHERE agent_id = ?
            """, (agent_id,)).fetchone()
        return {"total": row[0] or 0, "avg_score": round(row[1] or 0, 3), "avg_accesses": round(row[2] or 0, 1)}


class PersistentDecayAgent:
    def __init__(self, agent_id: str):
        self.client = anthropic.Anthropic()
        self.agent_id = agent_id
        self.store = SQLiteDecayStore(half_life_days=10.0)

    def remember(self, key: str, content: str, relevance: float = 1.0) -> None:
        self.store.store(self.agent_id, key, content, relevance)

    def chat(self, message: str) -> str:
        self.store.cleanup_expired(self.agent_id)  # Periodic cleanup
        memories = self.store.retrieve(self.agent_id, top_k=6)

        mem_text = "\n".join(
            f"- {m['key']} (score={m['score']}, age={m['age_days']}d): {m['content']}"
            for m in memories
        ) if memories else "No relevant memories."

        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=f"You are a helpful assistant.\nMemories:\n{mem_text}",
            messages=[{"role": "user", "content": message}]
        )
        return response.content[0].text


# Usage
agent = PersistentDecayAgent("user-42")
agent.remember("project", "Migrating legacy PHP app to FastAPI", relevance=0.9)
agent.remember("blocker", "Awaiting database credentials from ops team", relevance=0.7)
agent.remember("old_task", "Setup dev environment", relevance=0.3)

print("Stats:", agent.store.agent_stats("user-42"))
memories = agent.store.retrieve("user-42")
for m in memories:
    print(f"  {m['key']}: score={m['score']}, age={m['age_days']}d")

reply = agent.chat("What's blocking progress?")
print(reply)

# Expected Token Savings: Expired memories never injected into context; saves input tokens per call
# Environment: Production persistent agents, long-lived sessions, multi-session user assistants
```

### Option 5: Semantic Importance + Time Decay — Weight by both relevance and recency

```python
import anthropic
import json
import math
import time
from dataclasses import dataclass, field

HALF_LIFE_DAYS = 14.0
IMPORTANCE_WEIGHT = 0.4   # How much LLM-assigned importance affects score
TIME_WEIGHT = 0.6         # How much time decay affects score


@dataclass
class WeightedMemory:
    key: str
    content: str
    importance: float     # 0-1, assigned by LLM at storage time
    created_at: float = field(default_factory=time.time)

    def time_score(self) -> float:
        age_days = (time.time() - self.created_at) / 86400
        return math.exp(-math.log(2) * age_days / HALF_LIFE_DAYS)

    def combined_score(self) -> float:
        return IMPORTANCE_WEIGHT * self.importance + TIME_WEIGHT * self.time_score()


class ImportanceDecayStore:
    def __init__(self):
        self.client = anthropic.Anthropic()
        self._memories: dict[str, WeightedMemory] = {}

    def _assess_importance(self, content: str) -> float:
        """Ask Haiku to score how important this memory is to preserve."""
        response = self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=32,
            system="Score importance of this memory for a personal assistant (0.0-1.0). Return float only.",
            messages=[{"role": "user", "content": f"Memory: {content[:300]}\nImportance score:"}]
        )
        try:
            return min(1.0, max(0.0, float(response.content[0].text.strip())))
        except ValueError:
            return 0.5

    def store(self, key: str, content: str, importance: float | None = None) -> None:
        if importance is None:
            importance = self._assess_importance(content)
            print(f"[IMPORTANCE] '{key}' scored {importance:.2f}")
        self._memories[key] = WeightedMemory(key=key, content=content, importance=importance)

    def retrieve(self, top_k: int = 8, min_combined: float = 0.1) -> list[WeightedMemory]:
        alive = [m for m in self._memories.values() if m.combined_score() >= min_combined]
        return sorted(alive, key=lambda m: m.combined_score(), reverse=True)[:top_k]

    def memory_report(self) -> list[dict]:
        return [
            {
                "key": m.key,
                "importance": round(m.importance, 2),
                "time_score": round(m.time_score(), 2),
                "combined": round(m.combined_score(), 2),
                "age_days": round((time.time() - m.created_at) / 86400, 1),
            }
            for m in sorted(self._memories.values(), key=lambda m: m.combined_score(), reverse=True)
        ]


class ImportanceDecayAgent:
    def __init__(self):
        self.client = anthropic.Anthropic()
        self.memory = ImportanceDecayStore()

    def learn(self, key: str, fact: str) -> None:
        self.memory.store(key, fact)

    def chat(self, question: str) -> str:
        top_memories = self.memory.retrieve(top_k=6)
        context = "\n".join(
            f"- {m.key} (score={m.combined_score():.2f}): {m.content}"
            for m in top_memories
        )

        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=f"Assistant with weighted memory:\n{context}",
            messages=[{"role": "user", "content": question}]
        )
        return response.content[0].text


# Usage
agent = ImportanceDecayAgent()
agent.learn("user_name", "The user's name is Robert")         # High importance
agent.learn("today_task", "Fix bug in authentication module")  # Medium importance
agent.learn("weather_chat", "We chatted about the weather")    # Low importance

for item in agent.memory.memory_report():
    print(item)

reply = agent.chat("What should I focus on today?")
print(reply)

# Expected Token Savings: Low-importance memories decay faster; high-importance ones persist
# Environment: Personal productivity agents, tutors, assistants with user-specific long-term memory
```

### Option 6: Adaptive Half-Life — Tune decay rate based on memory category and user behavior

```python
import anthropic
import math
import time
from dataclasses import dataclass, field
from collections import defaultdict

# Category-specific half-lives (days)
CATEGORY_HALF_LIVES: dict[str, float] = {
    "user_identity": float("inf"),    # Never decays
    "user_preference": 90.0,
    "project_context": 21.0,
    "task_detail": 7.0,
    "conversation": 2.0,
    "ephemeral": 0.5,
}


@dataclass
class AdaptiveMemory:
    key: str
    content: str
    category: str
    created_at: float = field(default_factory=time.time)
    confirmed_at: float = field(default_factory=time.time)  # Last time user confirmed this is still true
    contradicted: bool = False

    def half_life(self) -> float:
        return CATEGORY_HALF_LIVES.get(self.category, 14.0)

    def score(self) -> float:
        if self.contradicted:
            return 0.0
        hl = self.half_life()
        if math.isinf(hl):
            return 1.0
        # Use confirmed_at for decay (reset when user re-confirms)
        age_days = (time.time() - self.confirmed_at) / 86400
        return math.exp(-math.log(2) * age_days / hl)

    def confirm(self) -> None:
        """User or agent confirms this memory is still accurate."""
        self.confirmed_at = time.time()

    def contradict(self) -> None:
        """Mark this memory as outdated/wrong."""
        self.contradicted = True


class AdaptiveDecayStore:
    def __init__(self):
        self._store: dict[str, AdaptiveMemory] = {}
        self._category_counts: dict[str, int] = defaultdict(int)

    def store(self, key: str, content: str, category: str = "conversation") -> AdaptiveMemory:
        if key in self._store:
            existing = self._store[key]
            if existing.content != content:
                existing.contradict()  # Old value is now outdated
        mem = AdaptiveMemory(key=key, content=content, category=category)
        self._store[key] = mem
        self._category_counts[category] += 1
        return mem

    def confirm_memory(self, key: str) -> bool:
        if key in self._store:
            self._store[key].confirm()
            return True
        return False

    def contradict_memory(self, key: str) -> bool:
        if key in self._store:
            self._store[key].contradict()
            return True
        return False

    def retrieve(
        self,
        categories: list[str] | None = None,
        top_k: int = 10,
        min_score: float = 0.05,
    ) -> list[AdaptiveMemory]:
        memories = list(self._store.values())
        if categories:
            memories = [m for m in memories if m.category in categories]
        alive = [m for m in memories if m.score() >= min_score and not m.contradicted]
        return sorted(alive, key=lambda m: m.score(), reverse=True)[:top_k]

    def category_health(self) -> dict[str, dict]:
        health: dict[str, dict] = {}
        for mem in self._store.values():
            cat = mem.category
            if cat not in health:
                health[cat] = {"total": 0, "alive": 0, "avg_score": 0.0, "half_life_days": CATEGORY_HALF_LIVES.get(cat, 14.0)}
            health[cat]["total"] += 1
            score = mem.score()
            if score >= 0.05:
                health[cat]["alive"] += 1
                health[cat]["avg_score"] += score
        for cat in health:
            if health[cat]["alive"]:
                health[cat]["avg_score"] = round(health[cat]["avg_score"] / health[cat]["alive"], 3)
        return health


class AdaptiveDecayAgent:
    def __init__(self):
        self.client = anthropic.Anthropic()
        self.store = AdaptiveDecayStore()

    def chat(self, message: str) -> str:
        # Load identity + preference + current project context
        memories = self.store.retrieve(
            categories=["user_identity", "user_preference", "project_context", "task_detail"],
            top_k=8
        )
        context = "\n".join(
            f"[{m.category}] {m.key}: {m.content} (score={m.score():.2f})"
            for m in memories
        ) if memories else "No memories."

        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=f"You are an adaptive assistant.\nContext:\n{context}",
            messages=[{"role": "user", "content": message}]
        )
        return response.content[0].text


# Usage
agent = AdaptiveDecayAgent()
agent.store.store("name", "Jordan", "user_identity")
agent.store.store("editor", "VS Code with Vim keybindings", "user_preference")
agent.store.store("active_project", "Rewriting auth service", "project_context")
agent.store.store("current_task", "Fix token expiry bug", "task_detail")
agent.store.store("yesterday_chat", "Discussed deployment pipeline", "conversation")

print("Category health:", agent.store.category_health())

# Simulate user contradicting old preference
agent.store.contradict_memory("editor")
agent.store.store("editor", "Neovim with custom config", "user_preference")

memories = agent.store.retrieve(top_k=10)
for m in memories:
    print(f"  [{m.category}] {m.key}: score={m.score():.2f}")

reply = agent.chat("What's my current setup and what should I work on?")
print(reply)

# Expected Token Savings: Category-aware decay; conversation memories clear in 2 days, identity never
# Environment: Sophisticated personal assistants, long-term user modeling, productivity agents
```

## Comparison

| Option | Decay Algorithm | Persistence | Access Reinforcement | Best For |
|--------|----------------|-------------|---------------------|----------|
| Exponential Decay | Single half-life | In-memory | No | Simple single-context agents |
| Tiered Decay | Per-tier half-lives | In-memory | No | Multi-category memory systems |
| Access-Reinforced | Decay reset on access | In-memory | Yes | Usage-based retention |
| SQLite Persistent | SQL decay expression | Disk | Yes | Production cross-session agents |
| Importance + Time | Dual-weight scoring | In-memory | No | LLM-assessed importance |
| Adaptive Half-Life | Category + contradiction | In-memory | Via contradiction | Sophisticated user modeling |
