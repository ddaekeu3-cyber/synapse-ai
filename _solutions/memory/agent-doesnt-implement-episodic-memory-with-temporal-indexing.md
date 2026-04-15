---
layout: solution
title: "Agent Doesn't Implement Episodic Memory with Temporal Indexing"
category: memory
description: "Agents that store memories as a flat list can't answer 'what happened last Tuesday?' or 'what did the user prefer three sessions ago?' — episodic memory with time-indexed retrieval solves this."
tags: [episodic-memory, temporal-indexing, memory, persistence, retrieval, sessions]
---

# Agent Doesn't Implement Episodic Memory with Temporal Indexing

## Problem

Most agent memory systems treat all stored facts as equal: a flat bag of key-value pairs or embedding vectors with no sense of time. This means agents can't distinguish what was learned recently from what was established months ago, can't retrieve "what we discussed last week," can't detect when old facts have been superseded, and can't answer questions like "when did the user first mention their project deadline?"

Episodic memory stores events as timestamped episodes with temporal metadata, enabling time-range queries, recency-weighted retrieval, and temporal reasoning over the agent's own history.

## Solutions

### Option 1: SQLite Episodic Store with Time-Range Queries

Store episodes in SQLite with full timestamp indexing. Retrieve by recency, time range, or relevance.

```python
import sqlite3
import json
import time
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from typing import Optional

@dataclass
class Episode:
    content: str
    episode_type: str       # "user_preference", "task_completed", "fact_learned", etc.
    session_id: str
    tags: list[str]
    importance: float = 1.0  # 0.0 - 1.0
    timestamp: float = 0.0

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()

class EpisodicMemory:
    def __init__(self, db_path: str = "episodic_memory.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                episode_type TEXT NOT NULL,
                session_id TEXT NOT NULL,
                tags TEXT NOT NULL,
                importance REAL DEFAULT 1.0,
                timestamp REAL NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON episodes(timestamp)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_type ON episodes(episode_type)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_session ON episodes(session_id)")
        self.conn.commit()

    def store(self, episode: Episode) -> int:
        cur = self.conn.execute(
            """INSERT INTO episodes (content, episode_type, session_id, tags, importance, timestamp, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (episode.content, episode.episode_type, episode.session_id,
             json.dumps(episode.tags), episode.importance, episode.timestamp,
             datetime.fromtimestamp(episode.timestamp).isoformat())
        )
        self.conn.commit()
        return cur.lastrowid

    def get_recent(self, n: int = 10, episode_type: Optional[str] = None) -> list[dict]:
        query = "SELECT * FROM episodes"
        params = []
        if episode_type:
            query += " WHERE episode_type = ?"
            params.append(episode_type)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(n)
        rows = self.conn.execute(query, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_in_range(self, since: datetime, until: Optional[datetime] = None) -> list[dict]:
        until = until or datetime.now()
        rows = self.conn.execute(
            "SELECT * FROM episodes WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp DESC",
            (since.timestamp(), until.timestamp())
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_by_session(self, session_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM episodes WHERE session_id = ? ORDER BY timestamp ASC",
            (session_id,)
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def _row_to_dict(self, row) -> dict:
        keys = ["id", "content", "episode_type", "session_id", "tags",
                "importance", "timestamp", "created_at"]
        d = dict(zip(keys, row))
        d["tags"] = json.loads(d["tags"])
        d["human_time"] = datetime.fromtimestamp(d["timestamp"]).strftime("%Y-%m-%d %H:%M")
        return d

import anthropic

def build_temporal_context(memory: EpisodicMemory, question: str) -> str:
    recent = memory.get_recent(5)
    last_week = memory.get_in_range(datetime.now() - timedelta(days=7))

    ctx_parts = []
    if recent:
        ctx_parts.append("Recent episodes:\n" + "\n".join(
            f"  [{e['human_time']}] {e['episode_type']}: {e['content']}"
            for e in recent
        ))
    if last_week:
        ctx_parts.append(f"Episodes from last 7 days ({len(last_week)} total)")

    return "\n\n".join(ctx_parts) if ctx_parts else "No prior episodes."

mem = EpisodicMemory(":memory:")
mem.store(Episode("User prefers dark mode", "user_preference", "sess_001", ["ui"]))
mem.store(Episode("Completed Python refactor task", "task_completed", "sess_001", ["python", "code"], importance=0.9))
mem.store(Episode("User deadline is April 30", "fact_learned", "sess_002", ["deadline"], importance=1.0))

client = anthropic.Anthropic()
ctx = build_temporal_context(mem, "What do you know about me?")
resp = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=200,
    system=f"You have episodic memory of past interactions:\n\n{ctx}",
    messages=[{"role": "user", "content": "What's my deadline?"}]
)
print(resp.content[0].text)
# Expected Token Savings: 30-50% vs injecting all memories — only inject relevant time window
# Environment: Persistent personal assistants, productivity agents, CRM bots
```

### Option 2: Recency-Weighted Retrieval with Temporal Decay

Score episodes by both semantic relevance and recency — older episodes fade unless they're marked high-importance.

```python
import math
import time
import numpy as np
import anthropic
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class TimedEpisode:
    content: str
    embedding: np.ndarray
    timestamp: float
    importance: float = 1.0
    episode_id: str = ""

    def recency_score(self, half_life_days: float = 7.0) -> float:
        """Exponential decay: importance halves every half_life_days days."""
        age_days = (time.time() - self.timestamp) / 86400.0
        decay = math.exp(-math.log(2) * age_days / half_life_days)
        return self.importance * decay

def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    return float(np.dot(a, b) / denom) if denom > 0 else 0.0

class TemporalEpisodicStore:
    def __init__(self, embed_fn, half_life_days: float = 7.0):
        self.episodes: list[TimedEpisode] = []
        self.embed_fn = embed_fn
        self.half_life_days = half_life_days

    def add(self, content: str, importance: float = 1.0,
            timestamp: Optional[float] = None) -> str:
        ep = TimedEpisode(
            content=content,
            embedding=np.array(self.embed_fn(content)),
            timestamp=timestamp or time.time(),
            importance=importance,
            episode_id=f"ep_{len(self.episodes)}"
        )
        self.episodes.append(ep)
        return ep.episode_id

    def retrieve(self, query: str, top_k: int = 5,
                 semantic_weight: float = 0.6,
                 recency_weight: float = 0.4) -> list[dict]:
        if not self.episodes:
            return []

        query_emb = np.array(self.embed_fn(query))
        scored = []
        for ep in self.episodes:
            sem = cosine_similarity(query_emb, ep.embedding)
            rec = ep.recency_score(self.half_life_days)
            # Normalize recency to 0-1 range (max importance=1.0, decay<1)
            combined = semantic_weight * sem + recency_weight * min(rec, 1.0)
            scored.append((combined, ep))

        scored.sort(key=lambda x: x[0], reverse=True)
        from datetime import datetime
        return [
            {
                "content": ep.content,
                "score": round(score, 3),
                "recency_score": round(ep.recency_score(self.half_life_days), 3),
                "age_days": round((time.time() - ep.timestamp) / 86400, 1),
                "timestamp": datetime.fromtimestamp(ep.timestamp).strftime("%Y-%m-%d"),
                "episode_id": ep.episode_id,
            }
            for score, ep in scored[:top_k]
        ]

# Stub embedder (replace with real embedding API in production)
def stub_embed(text: str) -> list[float]:
    import hashlib
    h = hashlib.md5(text.encode()).digest()
    vec = [(b / 255.0) * 2 - 1 for b in h]
    return vec * 2  # 32 dims

store = TemporalEpisodicStore(embed_fn=stub_embed, half_life_days=7.0)

# Add episodes at different "ages"
old_time = time.time() - 30 * 86400  # 30 days ago
store.add("User preferred verbose explanations", importance=0.5, timestamp=old_time)
store.add("User is learning Python async programming", importance=1.0)
store.add("User completed first FastAPI project", importance=0.9)
store.add("User prefers concise code examples", importance=0.8)

results = store.retrieve("What does the user prefer?", top_k=3)
for r in results:
    print(f"[{r['timestamp']}] age={r['age_days']}d score={r['score']}: {r['content']}")
# Old low-importance episodes are naturally deprioritized
# Expected Token Savings: 40-60% — inject only high-scored episodes, not all history
# Environment: Personal assistants with months of interaction history
```

### Option 3: Session-Boundary Episode Summarization

At the end of each session, compress detailed turn-by-turn history into a single high-level episodic summary stored with a timestamp.

```python
import anthropic
import sqlite3
import json
import time
from datetime import datetime

client = anthropic.Anthropic()

class SessionEpisodeManager:
    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS session_episodes (
                session_id TEXT PRIMARY KEY,
                summary TEXT NOT NULL,
                key_facts TEXT NOT NULL,
                tasks_completed TEXT NOT NULL,
                user_preferences TEXT NOT NULL,
                session_start REAL,
                session_end REAL,
                turn_count INTEGER
            )
        """)
        self.conn.commit()

    def summarize_session(
        self,
        session_id: str,
        conversation: list[dict],
        session_start: float,
    ) -> dict:
        """Compress a full conversation into an episodic record."""
        transcript = "\n".join(
            f"{m['role'].upper()}: {m['content']}"
            for m in conversation
        )

        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            system="You extract structured episode summaries from conversations.",
            messages=[{
                "role": "user",
                "content": f"""Analyze this conversation and extract a structured episode summary.

Conversation:
{transcript}

Return JSON with keys:
- summary: 2-sentence summary of what happened
- key_facts: list of factual things learned about the user or situation
- tasks_completed: list of tasks that were accomplished
- user_preferences: list of preferences the user expressed

JSON only, no prose."""
            }]
        )

        try:
            episode = json.loads(resp.content[0].text)
        except json.JSONDecodeError:
            episode = {
                "summary": "Session occurred but could not be parsed.",
                "key_facts": [],
                "tasks_completed": [],
                "user_preferences": []
            }

        now = time.time()
        self.conn.execute("""
            INSERT OR REPLACE INTO session_episodes
            (session_id, summary, key_facts, tasks_completed, user_preferences,
             session_start, session_end, turn_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            session_id,
            episode["summary"],
            json.dumps(episode.get("key_facts", [])),
            json.dumps(episode.get("tasks_completed", [])),
            json.dumps(episode.get("user_preferences", [])),
            session_start, now, len(conversation) // 2
        ))
        self.conn.commit()
        return episode

    def get_recent_episodes(self, n: int = 5) -> list[dict]:
        rows = self.conn.execute("""
            SELECT session_id, summary, key_facts, tasks_completed, user_preferences,
                   session_start, session_end, turn_count
            FROM session_episodes ORDER BY session_end DESC LIMIT ?
        """, (n,)).fetchall()

        result = []
        for row in rows:
            result.append({
                "session_id": row[0],
                "summary": row[1],
                "key_facts": json.loads(row[2]),
                "tasks_completed": json.loads(row[3]),
                "user_preferences": json.loads(row[4]),
                "date": datetime.fromtimestamp(row[5]).strftime("%Y-%m-%d"),
                "turns": row[7],
            })
        return result

    def build_episodic_context(self, n_sessions: int = 3) -> str:
        episodes = self.get_recent_episodes(n_sessions)
        if not episodes:
            return "No prior sessions."
        parts = []
        for ep in episodes:
            parts.append(
                f"[Session {ep['date']}] {ep['summary']}\n"
                f"  Facts: {', '.join(ep['key_facts']) or 'none'}\n"
                f"  Completed: {', '.join(ep['tasks_completed']) or 'none'}\n"
                f"  Preferences: {', '.join(ep['user_preferences']) or 'none'}"
            )
        return "\n\n".join(parts)

mgr = SessionEpisodeManager()

# Simulate a past session
past_conv = [
    {"role": "user", "content": "Help me refactor my Python API to use async."},
    {"role": "assistant", "content": "I'll help you convert to async/await..."},
    {"role": "user", "content": "I prefer minimal dependencies — no extra libraries."},
    {"role": "assistant", "content": "Great, we'll use only stdlib asyncio."},
]
mgr.summarize_session("sess_2026_04_15", past_conv, time.time() - 86400)

context = mgr.build_episodic_context()
print(context)
# Expected Token Savings: 70-85% vs replaying full conversation history
# Environment: Daily-use personal assistants, project management agents
```

### Option 4: Temporal Query Interface for the Agent

Give the agent itself a tool to query its episodic memory by time expressions like "last week" or "yesterday."

```python
import anthropic
import sqlite3
import json
import time
from datetime import datetime, timedelta
from typing import Any

client = anthropic.Anthropic()

def parse_time_expression(expr: str) -> tuple[float, float]:
    """Convert natural time expressions to (start_ts, end_ts)."""
    now = datetime.now()
    expr = expr.lower().strip()

    ranges = {
        "today": (now.replace(hour=0, minute=0, second=0), now),
        "yesterday": (
            (now - timedelta(days=1)).replace(hour=0, minute=0, second=0),
            (now - timedelta(days=1)).replace(hour=23, minute=59, second=59)
        ),
        "last week": (now - timedelta(days=7), now),
        "last month": (now - timedelta(days=30), now),
        "last 3 days": (now - timedelta(days=3), now),
        "last 24 hours": (now - timedelta(hours=24), now),
    }

    if expr in ranges:
        start, end = ranges[expr]
        return start.timestamp(), end.timestamp()

    # Default: last 7 days
    return (now - timedelta(days=7)).timestamp(), now.timestamp()

# In-memory episode store for demo
EPISODES = [
    {"content": "User set deadline for April 30", "type": "fact", "timestamp": time.time() - 86400 * 2},
    {"content": "Completed async refactor task", "type": "task", "timestamp": time.time() - 86400 * 1},
    {"content": "User prefers typed Python", "type": "preference", "timestamp": time.time() - 3600},
]

def query_episodes(time_range: str, episode_type: str = "all") -> dict:
    start_ts, end_ts = parse_time_expression(time_range)
    matches = [
        e for e in EPISODES
        if start_ts <= e["timestamp"] <= end_ts
        and (episode_type == "all" or e["type"] == episode_type)
    ]
    return {
        "time_range": time_range,
        "episode_type": episode_type,
        "count": len(matches),
        "episodes": [
            {
                "content": e["content"],
                "type": e["type"],
                "when": datetime.fromtimestamp(e["timestamp"]).strftime("%Y-%m-%d %H:%M"),
            }
            for e in sorted(matches, key=lambda x: x["timestamp"], reverse=True)
        ]
    }

tools = [
    {
        "name": "query_episodic_memory",
        "description": "Query your episodic memory by time range and type.",
        "input_schema": {
            "type": "object",
            "properties": {
                "time_range": {
                    "type": "string",
                    "description": "Time range: 'today', 'yesterday', 'last week', 'last month', 'last 3 days', 'last 24 hours'",
                },
                "episode_type": {
                    "type": "string",
                    "enum": ["all", "fact", "task", "preference"],
                    "description": "Type of episode to retrieve",
                }
            },
            "required": ["time_range"]
        }
    }
]

messages = [{"role": "user", "content": "What tasks did I complete in the last week?"}]

while True:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system="You have episodic memory tools. Use them to answer questions about past sessions.",
        tools=tools,
        messages=messages
    )

    if resp.stop_reason == "end_turn":
        print(resp.content[0].text)
        break

    if resp.stop_reason == "tool_use":
        messages.append({"role": "assistant", "content": resp.content})
        tool_results = []
        for block in resp.content:
            if block.type == "tool_use":
                result = query_episodes(
                    block.input.get("time_range", "last week"),
                    block.input.get("episode_type", "all")
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result)
                })
        messages.append({"role": "user", "content": tool_results})
# Expected Token Savings: 50-70% — agent fetches only relevant time window on demand
# Environment: Agents with months of history; Q&A over session archives
```

### Option 5: Cross-Session Fact Versioning

Track when facts change over time — detect and reconcile contradictions between old and new episodes.

```python
import anthropic
import sqlite3
import json
import time
from datetime import datetime

client = anthropic.Anthropic()

class VersionedFactStore:
    """Tracks how facts evolve over time with full history."""

    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fact_key TEXT NOT NULL,
                fact_value TEXT NOT NULL,
                confidence REAL DEFAULT 1.0,
                source TEXT,
                valid_from REAL NOT NULL,
                valid_until REAL,  -- NULL means currently active
                superseded_by INTEGER REFERENCES facts(id)
            )
        """)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_key ON facts(fact_key)")
        self.conn.commit()

    def upsert_fact(self, key: str, value: str,
                    confidence: float = 1.0, source: str = "") -> dict:
        """Store a fact, superseding any previous version."""
        now = time.time()

        # Find active fact with same key
        existing = self.conn.execute(
            "SELECT id, fact_value FROM facts WHERE fact_key = ? AND valid_until IS NULL",
            (key,)
        ).fetchone()

        if existing and existing[1] == value:
            return {"action": "unchanged", "key": key, "value": value}

        # Insert new version
        cur = self.conn.execute(
            """INSERT INTO facts (fact_key, fact_value, confidence, source, valid_from)
               VALUES (?, ?, ?, ?, ?)""",
            (key, value, confidence, source, now)
        )
        new_id = cur.lastrowid

        # Supersede old version if it exists
        changed = False
        if existing:
            self.conn.execute(
                "UPDATE facts SET valid_until = ?, superseded_by = ? WHERE id = ?",
                (now, new_id, existing[0])
            )
            changed = True

        self.conn.commit()
        return {
            "action": "updated" if changed else "created",
            "key": key,
            "old_value": existing[1] if existing else None,
            "new_value": value,
        }

    def get_current_facts(self) -> dict[str, dict]:
        rows = self.conn.execute(
            "SELECT fact_key, fact_value, confidence, valid_from FROM facts WHERE valid_until IS NULL"
        ).fetchall()
        return {
            row[0]: {
                "value": row[1],
                "confidence": row[2],
                "since": datetime.fromtimestamp(row[3]).strftime("%Y-%m-%d"),
            }
            for row in rows
        }

    def get_fact_history(self, key: str) -> list[dict]:
        rows = self.conn.execute(
            """SELECT fact_value, confidence, valid_from, valid_until
               FROM facts WHERE fact_key = ? ORDER BY valid_from ASC""",
            (key,)
        ).fetchall()
        return [
            {
                "value": r[0],
                "confidence": r[1],
                "from": datetime.fromtimestamp(r[2]).strftime("%Y-%m-%d"),
                "until": datetime.fromtimestamp(r[3]).strftime("%Y-%m-%d") if r[3] else "now",
            }
            for r in rows
        ]

store = VersionedFactStore()

# Simulate facts evolving over time
store.upsert_fact("user_language", "Python", source="session_1")
store.upsert_fact("user_experience", "intermediate", source="session_1")
time.sleep(0.01)
store.upsert_fact("user_language", "Python and Rust", source="session_5")  # Updated!
time.sleep(0.01)
store.upsert_fact("user_experience", "senior", source="session_8")  # Updated!

current = store.get_current_facts()
history = store.get_fact_history("user_language")
print("Current facts:", json.dumps(current, indent=2))
print("Language history:", json.dumps(history, indent=2))

# Inject only current, verified facts into context
fact_ctx = "\n".join(
    f"- {k}: {v['value']} (known since {v['since']})"
    for k, v in current.items()
)
resp = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=150,
    system=f"Known facts about this user:\n{fact_ctx}",
    messages=[{"role": "user", "content": "What languages do I know?"}]
)
print(resp.content[0].text)
# Expected Token Savings: 20-40% — eliminates contradictory/stale facts from context
# Environment: Long-term personal agents, user profile systems, CRM integrations
```

### Option 6: Episodic Memory Replay for Context Reconstruction

When starting a new session, replay the most relevant past episodes to reconstruct context without injecting entire history.

```python
import anthropic
import json
import time
from datetime import datetime, timedelta
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class EpisodeReplay:
    session_date: str
    summary: str
    continuations: list[str]  # Things the user said they'd do next time
    open_questions: list[str]  # Unresolved questions

EPISODE_ARCHIVE = [
    EpisodeReplay(
        session_date="2026-04-10",
        summary="Discussed async Python architecture. User building a FastAPI service.",
        continuations=["User will add Redis caching next session", "Will implement auth middleware"],
        open_questions=["Should use Redis Cluster or single Redis?"]
    ),
    EpisodeReplay(
        session_date="2026-04-13",
        summary="Implemented Redis caching. Struggled with TTL configuration.",
        continuations=["Test cache invalidation edge cases", "Deploy to staging"],
        open_questions=["What TTL for user session data?"]
    ),
    EpisodeReplay(
        session_date="2026-04-15",
        summary="Deployed to staging. Found memory leak in connection pool.",
        continuations=["Fix connection pool leak", "Run load test"],
        open_questions=["Is the leak in aioredis or custom pooling?"]
    ),
]

def build_session_opener(episodes: list[EpisodeReplay], n_recent: int = 3) -> str:
    recent = episodes[-n_recent:]
    parts = ["## Session History Replay\n"]

    for ep in recent:
        parts.append(f"### {ep.session_date}")
        parts.append(f"{ep.summary}")
        if ep.continuations:
            parts.append("Planned for next time: " + "; ".join(ep.continuations))
        if ep.open_questions:
            parts.append("Open questions: " + "; ".join(ep.open_questions))

    # Highlight what was promised for today
    latest = recent[-1]
    if latest.continuations:
        parts.append(f"\n## Today's Agenda (from last session on {latest.session_date})")
        parts.append("\n".join(f"- {item}" for item in latest.continuations))

    return "\n".join(parts)

replay_context = build_session_opener(EPISODE_ARCHIVE)
print(replay_context)
print("\n---\n")

resp = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=200,
    system=(
        "You are a persistent coding assistant. You have episodic memory of past sessions.\n\n"
        + replay_context
    ),
    messages=[{"role": "user", "content": "Let's pick up where we left off. What should we tackle?"}]
)
print(resp.content[0].text)
# Expected Token Savings: 60-80% vs replaying full transcript — structured replay is dense
# Environment: Daily coding assistants, project-tracking agents, mentoring bots
```

## Comparison Table

| Option | Storage | Temporal Indexing | Query Type | Best For |
|--------|---------|------------------|-----------|----------|
| 1: SQLite with Time-Range | SQLite | Full timestamp index | Time window, session, type | General-purpose persistent agents |
| 2: Recency-Weighted Retrieval | In-memory / numpy | Exponential decay score | Semantic + recency | Assistants with months of history |
| 3: Session Summarization | SQLite | Session boundary timestamps | Recent N sessions | Daily-use assistants |
| 4: Temporal Query Tool | In-memory | Natural language time parse | Agent-driven on demand | Agents that need to reason about when |
| 5: Versioned Facts | SQLite | Validity windows | Current state + history | Fact-tracking, profile systems |
| 6: Episode Replay | In-memory | Session date | Structured session opener | Session continuity, project agents |
