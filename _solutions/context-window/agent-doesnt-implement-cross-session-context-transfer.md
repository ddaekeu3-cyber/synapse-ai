---
title: "Agent Doesn't Implement Cross-Session Context Transfer"
description: "How to preserve and transfer relevant context from previous sessions into new ones, so the agent doesn't start from scratch on every conversation."
categories: [context-window]
difficulty: intermediate
---

When a user returns after a break, the agent has no memory of what was discussed, decided, or accomplished. Rebuilding context from scratch wastes tokens and frustrates users. Cross-session context transfer selectively carries forward the most relevant information from past sessions.

## Solution 1: Session Summary Injection

At the end of each session, generate a compact summary and inject it at the start of the next session.

```python
import asyncio
import json
import time
from pathlib import Path
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-sonnet-4-6"
SUMMARY_MODEL = "claude-haiku-4-5-20251001"
SESSION_DIR = Path("/tmp/agent_sessions")
SESSION_DIR.mkdir(exist_ok=True)


async def summarize_session(messages: list[dict]) -> str:
    history = "\n".join(
        f"{m['role'].upper()}: {m['content'] if isinstance(m['content'], str) else '[tool interaction]'}"
        for m in messages
        if isinstance(m.get("content"), str)
    )
    resp = await client.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=400,
        messages=[
            {
                "role": "user",
                "content": (
                    "Summarize this conversation session in ≤200 words. "
                    "Focus on: decisions made, tasks completed, open questions, user preferences.\n\n"
                    + history
                ),
            }
        ],
    )
    return resp.content[0].text


def save_session(session_id: str, messages: list[dict], summary: str):
    data = {
        "session_id": session_id,
        "ended_at": time.time(),
        "summary": summary,
        "message_count": len(messages),
    }
    (SESSION_DIR / f"{session_id}.json").write_text(json.dumps(data))


def load_recent_sessions(user_id: str, n: int = 3) -> list[dict]:
    sessions = sorted(SESSION_DIR.glob(f"{user_id}_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    result = []
    for path in sessions[:n]:
        try:
            result.append(json.loads(path.read_text()))
        except Exception:
            pass
    return result


def build_context_injection(sessions: list[dict]) -> str:
    if not sessions:
        return ""
    parts = ["[Previous session context]"]
    for s in sessions:
        ts = time.strftime("%Y-%m-%d", time.gmtime(s["ended_at"]))
        parts.append(f"\nSession {ts}:\n{s['summary']}")
    return "\n".join(parts)


async def run_session(user_id: str, new_query: str) -> str:
    session_id = f"{user_id}_{int(time.time())}"

    # Load context from previous sessions
    past_sessions = load_recent_sessions(user_id)
    context = build_context_injection(past_sessions)

    # Build initial messages
    messages = []
    if context:
        messages.append({"role": "user", "content": context})
        messages.append({"role": "assistant", "content": "I have the context from our previous sessions."})

    messages.append({"role": "user", "content": new_query})

    resp = await client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=messages,
    )
    reply = resp.content[0].text
    messages.append({"role": "assistant", "content": reply})

    # Save summary for future sessions
    summary = await summarize_session(messages)
    save_session(session_id, messages, summary)

    return reply


async def main():
    # Session 1
    reply1 = await run_session("user_42", "I'm building a FastAPI service for order management. What's a good structure?")
    print(f"Session 1: {reply1[:200]}…\n")

    # Session 2 — agent should know about the FastAPI project
    reply2 = await run_session("user_42", "How should I handle database migrations in the project we discussed?")
    print(f"Session 2: {reply2[:200]}…")


asyncio.run(main())
```

## Solution 2: Selective Fact Extraction with Structured Store

Extract structured facts (preferences, decisions, entities) from each session and store them in a typed registry.

```python
import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
import anthropic

client = anthropic.AsyncAnthropic()
EXTRACTOR_MODEL = "claude-haiku-4-5-20251001"
STORE_PATH = Path("/tmp/agent_facts.json")


@dataclass
class Fact:
    category: str   # preference | decision | entity | constraint | open_question
    content: str
    confidence: float
    session_id: str
    timestamp: float = field(default_factory=time.time)


def load_facts(user_id: str) -> list[Fact]:
    if not STORE_PATH.exists():
        return []
    try:
        data = json.loads(STORE_PATH.read_text())
        return [Fact(**f) for f in data.get(user_id, [])]
    except Exception:
        return []


def save_facts(user_id: str, facts: list[Fact]):
    data = {}
    if STORE_PATH.exists():
        try:
            data = json.loads(STORE_PATH.read_text())
        except Exception:
            pass
    data[user_id] = [f.__dict__ for f in facts]
    STORE_PATH.write_text(json.dumps(data, indent=2))


async def extract_facts(session_text: str, session_id: str) -> list[Fact]:
    resp = await client.messages.create(
        model=EXTRACTOR_MODEL,
        max_tokens=500,
        messages=[
            {
                "role": "user",
                "content": (
                    "Extract structured facts from this conversation. "
                    "Categories: preference, decision, entity, constraint, open_question.\n\n"
                    f"Conversation:\n{session_text}\n\n"
                    "Return JSON array: [{\"category\": str, \"content\": str, \"confidence\": 0-1}]"
                ),
            }
        ],
    )
    try:
        raw = json.loads(resp.content[0].text)
        return [
            Fact(
                category=f.get("category", "entity"),
                content=f.get("content", ""),
                confidence=float(f.get("confidence", 0.8)),
                session_id=session_id,
            )
            for f in raw
        ]
    except Exception:
        return []


def build_fact_context(facts: list[Fact], max_facts: int = 20) -> str:
    if not facts:
        return ""
    # Sort by confidence desc, then recency
    sorted_facts = sorted(facts, key=lambda f: (f.confidence, f.timestamp), reverse=True)
    top = sorted_facts[:max_facts]

    by_category: dict[str, list[str]] = {}
    for f in top:
        by_category.setdefault(f.category, []).append(f.content)

    lines = ["[Remembered context from previous sessions]"]
    for cat, items in by_category.items():
        lines.append(f"\n{cat.upper()}:")
        for item in items:
            lines.append(f"  • {item}")

    return "\n".join(lines)


async def main():
    user_id = "user_99"
    session_id = "sess_001"

    # Simulate a conversation
    session_text = """
USER: I prefer TypeScript over JavaScript for all my projects.
ASSISTANT: Understood, I'll use TypeScript.
USER: We decided to use PostgreSQL as the primary database.
ASSISTANT: Great choice. PostgreSQL offers strong ACID guarantees.
USER: The API must support multi-tenancy. That's a hard constraint.
ASSISTANT: I'll ensure all queries are tenant-scoped.
"""

    new_facts = await extract_facts(session_text, session_id)
    existing_facts = load_facts(user_id)
    all_facts = existing_facts + new_facts
    save_facts(user_id, all_facts)

    print("Extracted facts:")
    for f in new_facts:
        print(f"  [{f.category}] {f.content} (confidence={f.confidence:.0%})")

    print("\nContext for next session:")
    print(build_fact_context(all_facts))


asyncio.run(main())
```

## Solution 3: Relevance-Filtered Context Retrieval

Store all session data and retrieve only the chunks most relevant to the current query.

```python
import asyncio
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
import anthropic

client = anthropic.AsyncAnthropic()
RANKER_MODEL = "claude-haiku-4-5-20251001"
CHUNKS_PATH = Path("/tmp/session_chunks.json")


@dataclass
class ContextChunk:
    text: str
    session_id: str
    chunk_type: str  # summary | decision | preference | code


def load_chunks(user_id: str) -> list[ContextChunk]:
    if not CHUNKS_PATH.exists():
        return []
    try:
        data = json.loads(CHUNKS_PATH.read_text())
        return [ContextChunk(**c) for c in data.get(user_id, [])]
    except Exception:
        return []


def save_chunk(user_id: str, chunk: ContextChunk):
    data = {}
    if CHUNKS_PATH.exists():
        try:
            data = json.loads(CHUNKS_PATH.read_text())
        except Exception:
            pass
    data.setdefault(user_id, []).append(chunk.__dict__)
    CHUNKS_PATH.write_text(json.dumps(data, indent=2))


def tfidf_score(query: str, chunk: str) -> float:
    query_words = set(query.lower().split())
    chunk_words = chunk.lower().split()
    if not chunk_words:
        return 0.0
    tf = sum(1 for w in chunk_words if w in query_words) / len(chunk_words)
    return tf * math.log(1 + len(query_words))


async def retrieve_relevant_chunks(
    query: str, chunks: list[ContextChunk], top_k: int = 5
) -> list[ContextChunk]:
    if not chunks:
        return []

    # Score all chunks
    scored = [(chunk, tfidf_score(query, chunk.text)) for chunk in chunks]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [chunk for chunk, _ in scored[:top_k] if _ > 0]


async def build_relevant_context(query: str, user_id: str) -> str:
    chunks = load_chunks(user_id)
    relevant = await retrieve_relevant_chunks(query, chunks)

    if not relevant:
        return ""

    parts = ["[Relevant context from past sessions]"]
    for c in relevant:
        parts.append(f"\n[{c.chunk_type.upper()} from {c.session_id}]\n{c.text}")

    return "\n".join(parts)


async def main():
    user_id = "user_55"

    # Store some past session chunks
    chunks_to_store = [
        ContextChunk("User prefers async Python with FastAPI.", "sess_1", "preference"),
        ContextChunk("Decided to use Redis for session storage.", "sess_1", "decision"),
        ContextChunk("The project uses PostgreSQL 15 with pgvector.", "sess_2", "decision"),
        ContextChunk("User asked about rate limiting strategies.", "sess_2", "summary"),
        ContextChunk("Implemented JWT auth with 15-minute expiry.", "sess_3", "decision"),
    ]
    for c in chunks_to_store:
        save_chunk(user_id, c)

    # New query
    query = "How should I add database connection pooling to our FastAPI project?"
    context = await build_relevant_context(query, user_id)

    print(f"Query: {query}\n")
    print(context if context else "[No relevant past context found]")


asyncio.run(main())
```

## Solution 4: Tiered Context Transfer (Hot / Warm / Cold)

Organize past context into tiers: always inject "hot" facts, selectively inject "warm" summaries, skip "cold" details.

```python
import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
import anthropic

client = anthropic.AsyncAnthropic()
TIERS_PATH = Path("/tmp/context_tiers.json")

HOT_MAX_TOKENS = 500      # Always injected
WARM_MAX_TOKENS = 1000    # Injected if relevant
COLD_MAX_TOKENS = 0       # Never injected (on-demand retrieval only)


@dataclass
class TieredContext:
    hot: list[str] = field(default_factory=list)    # User preferences, standing constraints
    warm: list[str] = field(default_factory=list)   # Recent decisions, active tasks
    cold: list[str] = field(default_factory=list)   # Old details, historical data


def load_tiers(user_id: str) -> TieredContext:
    if not TIERS_PATH.exists():
        return TieredContext()
    try:
        data = json.loads(TIERS_PATH.read_text())
        raw = data.get(user_id, {})
        return TieredContext(
            hot=raw.get("hot", []),
            warm=raw.get("warm", []),
            cold=raw.get("cold", []),
        )
    except Exception:
        return TieredContext()


def save_tiers(user_id: str, tiers: TieredContext):
    data = {}
    if TIERS_PATH.exists():
        try:
            data = json.loads(TIERS_PATH.read_text())
        except Exception:
            pass
    data[user_id] = {"hot": tiers.hot, "warm": tiers.warm, "cold": tiers.cold}
    TIERS_PATH.write_text(json.dumps(data, indent=2))


def promote_to_cold(tiers: TieredContext):
    """Age warm items to cold, keep most recent N warm items."""
    max_warm = 10
    if len(tiers.warm) > max_warm:
        overflow = tiers.warm[:-max_warm]
        tiers.cold.extend(overflow)
        tiers.warm = tiers.warm[-max_warm:]
        tiers.cold = tiers.cold[-50:]  # cap cold at 50


def build_injection(tiers: TieredContext, include_warm: bool = True) -> str:
    parts = []
    if tiers.hot:
        parts.append("[Standing context — always applies]")
        for item in tiers.hot:
            parts.append(f"  • {item}")

    if include_warm and tiers.warm:
        parts.append("\n[Recent session context]")
        for item in tiers.warm[-5:]:  # Last 5 warm items
            parts.append(f"  • {item}")

    return "\n".join(parts)


async def main():
    user_id = "user_77"
    tiers = load_tiers(user_id)

    # Simulate adding context
    tiers.hot.extend([
        "User works in Python only.",
        "All code must be async.",
        "Use type hints everywhere.",
    ])
    tiers.warm.extend([
        "Currently building an order management API.",
        "Decided on PostgreSQL + SQLAlchemy.",
        "In progress: implementing webhook delivery.",
    ])

    promote_to_cold(tiers)
    save_tiers(user_id, tiers)

    injection = build_injection(tiers)
    print("Context to inject into next session:\n")
    print(injection)

    print(f"\nToken estimate: ~{len(injection) // 4} tokens")


asyncio.run(main())
```

## Solution 5: Differential Context Update

Only transfer what changed between sessions, not the full accumulated history.

```python
import asyncio
import json
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
import anthropic

client = anthropic.AsyncAnthropic()
DIFF_MODEL = "claude-haiku-4-5-20251001"
SNAPSHOT_PATH = Path("/tmp/session_snapshots.json")


@dataclass
class ContextSnapshot:
    session_id: str
    facts: dict[str, str]  # fact_id -> content
    checksum: str


def fact_checksum(facts: dict[str, str]) -> str:
    return hashlib.md5(json.dumps(facts, sort_keys=True).encode()).hexdigest()


def load_snapshot(user_id: str) -> ContextSnapshot | None:
    if not SNAPSHOT_PATH.exists():
        return None
    try:
        data = json.loads(SNAPSHOT_PATH.read_text())
        raw = data.get(user_id)
        if raw:
            return ContextSnapshot(**raw)
    except Exception:
        pass
    return None


def save_snapshot(user_id: str, snapshot: ContextSnapshot):
    data = {}
    if SNAPSHOT_PATH.exists():
        try:
            data = json.loads(SNAPSHOT_PATH.read_text())
        except Exception:
            pass
    data[user_id] = snapshot.__dict__
    SNAPSHOT_PATH.write_text(json.dumps(data, indent=2))


async def compute_diff(old: dict[str, str], new: dict[str, str]) -> dict:
    added = {k: v for k, v in new.items() if k not in old}
    removed = {k: v for k, v in old.items() if k not in new}
    changed = {k: (old[k], new[k]) for k in new if k in old and old[k] != new[k]}
    return {"added": added, "removed": removed, "changed": changed}


def diff_to_context(diff: dict) -> str:
    parts = []
    if diff["added"]:
        parts.append("[New since last session]")
        for k, v in diff["added"].items():
            parts.append(f"  + {v}")
    if diff["changed"]:
        parts.append("[Updated since last session]")
        for k, (old, new) in diff["changed"].items():
            parts.append(f"  ~ {new} (was: {old})")
    if diff["removed"]:
        parts.append("[No longer applies]")
        for k, v in diff["removed"].items():
            parts.append(f"  - {v}")
    return "\n".join(parts)


async def update_and_inject(user_id: str, new_facts: dict[str, str]) -> str:
    old_snapshot = load_snapshot(user_id)

    new_snapshot = ContextSnapshot(
        session_id=f"sess_{len(new_facts)}",
        facts=new_facts,
        checksum=fact_checksum(new_facts),
    )

    if old_snapshot is None or old_snapshot.checksum == new_snapshot.checksum:
        # No change
        save_snapshot(user_id, new_snapshot)
        return "[No context changes since last session]"

    diff = await compute_diff(old_snapshot.facts, new_facts)
    save_snapshot(user_id, new_snapshot)
    return diff_to_context(diff)


async def main():
    user_id = "user_88"

    # Session 1 facts
    s1_facts = {
        "lang": "User prefers Python.",
        "db": "Using PostgreSQL.",
        "task": "Building order management API.",
    }
    msg1 = await update_and_inject(user_id, s1_facts)
    print(f"Session 1 injection:\n{msg1}\n")

    # Session 2 — task changed, new constraint added
    s2_facts = {
        "lang": "User prefers Python.",
        "db": "Using PostgreSQL.",
        "task": "Completing webhook delivery module.",  # Changed
        "auth": "JWT auth implemented.",                # New
    }
    msg2 = await update_and_inject(user_id, s2_facts)
    print(f"Session 2 injection:\n{msg2}")


asyncio.run(main())
```

## Solution 6: Multi-User Context Isolation with Transfer Gates

Support multi-user sessions with strict isolation, transferring context only when explicitly authorized.

```python
import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
import anthropic

client = anthropic.AsyncAnthropic()
VAULT_PATH = Path("/tmp/context_vault.json")


@dataclass
class UserContextVault:
    user_id: str
    data: dict[str, str] = field(default_factory=dict)
    shared_with: list[str] = field(default_factory=list)  # User IDs who can receive this context
    created_at: float = field(default_factory=time.time)


def load_vault(user_id: str) -> UserContextVault:
    if not VAULT_PATH.exists():
        return UserContextVault(user_id=user_id)
    try:
        data = json.loads(VAULT_PATH.read_text())
        raw = data.get(user_id)
        if raw:
            return UserContextVault(**raw)
    except Exception:
        pass
    return UserContextVault(user_id=user_id)


def save_vault(vault: UserContextVault):
    data = {}
    if VAULT_PATH.exists():
        try:
            data = json.loads(VAULT_PATH.read_text())
        except Exception:
            pass
    data[vault.user_id] = vault.__dict__
    VAULT_PATH.write_text(json.dumps(data, indent=2))


def transfer_context(from_user: str, to_user: str) -> str | None:
    """Transfer context from one user vault to another, if authorized."""
    source = load_vault(from_user)
    if to_user not in source.shared_with:
        return None  # Not authorized

    target = load_vault(to_user)
    transferred = []
    for key, value in source.data.items():
        if key not in target.data:  # Don't overwrite existing
            target.data[key] = value
            transferred.append(value)
    save_vault(target)

    return "\n".join(f"  • {t}" for t in transferred) if transferred else None


async def main():
    # Setup user A's context
    vault_a = load_vault("user_A")
    vault_a.data = {
        "project": "Building multi-tenant SaaS app.",
        "stack": "FastAPI + PostgreSQL + Redis.",
    }
    vault_a.shared_with = ["user_B"]  # Authorize B to receive context
    save_vault(vault_a)

    # User B starts a new session — receives context from A
    transferred = transfer_context("user_A", "user_B")
    if transferred:
        print(f"[Context transferred to user_B]\n{transferred}")
    else:
        print("[No authorized transfer]")

    # User C (unauthorized) tries to receive context from A
    transferred_c = transfer_context("user_A", "user_C")
    print(f"\n[Transfer to user_C]: {'blocked (not authorized)' if not transferred_c else transferred_c}")


asyncio.run(main())
```

## Comparison

| Solution | Storage | Relevance filtering | Token overhead | Best for |
|---|---|---|---|---|
| **Session summary injection** | File (JSON) | No (full summary) | Low-Medium | General-purpose chat agents |
| **Structured fact extraction** | File (typed facts) | By category | Low | Decision-heavy workflows |
| **Relevance-filtered retrieval** | File (chunks) | TF-IDF scoring | Low | Long-running projects |
| **Tiered hot/warm/cold** | File (tiers) | By tier | Very low | Frequent returning users |
| **Differential update** | File (snapshots) | Diff only | Minimal | High-frequency sessions |
| **Multi-user with gates** | File (vaults) | By authorization | Low | Multi-user or team agents |

Start with **session summary injection** (Solution 1) — simple, general-purpose, minimal overhead. Upgrade to **structured fact extraction** (Solution 2) when you need the agent to reliably recall specific preferences and decisions without reading full summaries.
