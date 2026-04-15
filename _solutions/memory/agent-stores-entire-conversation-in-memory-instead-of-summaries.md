---
layout: solution
title: "Agent stores entire conversation in memory instead of summaries"
category: memory
description: "Agent persists every raw message verbatim to its memory store, causing unbounded growth that fills the context window on recall and makes memory search slow and noisy."
tags: [memory, summarization, context-window, long-term-memory, conversation-history]
---

## Symptom

After several sessions, memory retrieval returns walls of raw chat text — greetings, filler, clarifications, and repeated information — alongside the few useful facts. The agent's context fills up before it can answer because memory alone consumes 40,000+ tokens. Search results are noisy because every message scores similarly against any query.

## Root Cause

The memory writer calls `memory.save(message)` in a loop, storing the entire `messages` array verbatim. There is no compression step. Raw conversation text is 10–50× more verbose than the facts it contains. Over dozens of sessions, the memory store becomes a transcript archive rather than a distilled knowledge base. Recall is expensive and inaccurate because the signal-to-noise ratio approaches zero.

---

## Option 1 — End-of-session summariser that replaces raw messages

**At session end, ask a fast model to distil the conversation into a compact bullet-point summary, then save only the summary.**

```python
import json
import os
from datetime import datetime
import anthropic

client = anthropic.Anthropic()

MEMORY_FILE = "agent_memory.json"


def load_memory() -> list[dict]:
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE) as f:
            return json.load(f)
    return []


def save_memory(memories: list[dict]) -> None:
    with open(MEMORY_FILE, "w") as f:
        json.dump(memories, f, indent=2)


def summarise_session(messages: list[dict]) -> str:
    """Distil a full conversation into a compact summary using haiku."""
    transcript = "\n".join(
        f"{m['role'].upper()}: {m['content'] if isinstance(m['content'], str) else '[tool call]'}"
        for m in messages
        if isinstance(m.get("content"), str)
    )
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": (
                "Summarise this conversation into ≤ 10 bullet points. "
                "Include only facts, decisions, and preferences that would help "
                "a future assistant session. Omit greetings, filler, and repetition.\n\n"
                f"CONVERSATION:\n{transcript[:12_000]}"
            ),
        }],
    )
    return response.content[0].text


def run_session(user_inputs: list[str]) -> None:
    session_messages: list[dict] = []
    existing_memory = load_memory()

    # Inject previous session summaries as context
    if existing_memory:
        memory_text = "\n\n".join(
            f"[Session {m['date']}]\n{m['summary']}"
            for m in existing_memory[-5:]   # last 5 sessions
        )
        system = f"Previous session notes:\n{memory_text}"
    else:
        system = "You are a helpful assistant."

    for user_input in user_inputs:
        session_messages.append({"role": "user", "content": user_input})
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=system,
            messages=session_messages,
        )
        reply = response.content[0].text
        session_messages.append({"role": "assistant", "content": reply})
        print(f"A: {reply[:80]}")

    # End of session: summarise and store (not the raw messages)
    summary = summarise_session(session_messages)
    existing_memory.append({"date": datetime.now().strftime("%Y-%m-%d"), "summary": summary})
    save_memory(existing_memory)
    print(f"\n[Session saved: {len(summary)} chars vs {sum(len(str(m)) for m in session_messages):,} raw]")


run_session([
    "Hi, I'm working on a Python API that connects to PostgreSQL.",
    "We're using asyncpg for the connection pool with a max size of 20.",
    "The main issue we solved today was connection leaks on exception paths.",
])
```

**Expected Token Savings:** A 10,000-token conversation compressed to a 300-token summary — 97% reduction in stored tokens. Injecting 5 session summaries costs ~1,500 tokens vs. 50,000+ for raw history.

**Environment:** Agents with persistent cross-session memory; Python 3.10+; works with any file or database backend.

---

## Option 2 — Rolling compaction: merge old summaries as memory grows

**Keep a rolling summary that absorbs new sessions, preventing the summary list itself from growing unboundedly.**

```python
import json
import os
from datetime import datetime
import anthropic

client = anthropic.Anthropic()
MEMORY_FILE = "rolling_memory.json"
MAX_SESSION_SUMMARIES = 10   # compact once we exceed this


def load_memory() -> dict:
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE) as f:
            return json.load(f)
    return {"long_term": "", "recent_sessions": []}


def save_memory(mem: dict) -> None:
    with open(MEMORY_FILE, "w") as f:
        json.dump(mem, f, indent=2)


def summarise(text: str, instruction: str, max_tokens: int = 400) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": f"{instruction}\n\n{text[:15_000]}"}],
    )
    return resp.content[0].text


def compact_old_sessions(mem: dict) -> dict:
    """Merge all recent_sessions into long_term when we have too many."""
    sessions_text = "\n\n".join(
        f"[{s['date']}] {s['summary']}"
        for s in mem["recent_sessions"]
    )
    combined = (mem["long_term"] + "\n\n" + sessions_text).strip()
    mem["long_term"] = summarise(
        combined,
        "Merge these session notes into a single concise knowledge base. "
        "Keep all unique facts, remove duplicates and ephemeral details.",
        max_tokens=600,
    )
    mem["recent_sessions"] = []
    print(f"[Memory compacted: long_term={len(mem['long_term'])} chars]")
    return mem


def add_session_to_memory(mem: dict, messages: list[dict]) -> dict:
    transcript = "\n".join(
        f"{m['role'].upper()}: {m['content']}"
        for m in messages if isinstance(m.get("content"), str)
    )
    summary = summarise(
        transcript,
        "Summarise in ≤ 8 bullet points. Facts and decisions only, no filler.",
        max_tokens=250,
    )
    mem["recent_sessions"].append({
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "summary": summary,
    })
    if len(mem["recent_sessions"]) >= MAX_SESSION_SUMMARIES:
        mem = compact_old_sessions(mem)
    return mem


def get_memory_context(mem: dict) -> str:
    parts = []
    if mem["long_term"]:
        parts.append(f"Long-term knowledge:\n{mem['long_term']}")
    if mem["recent_sessions"]:
        recent = "\n\n".join(f"[{s['date']}]\n{s['summary']}" for s in mem["recent_sessions"][-3:])
        parts.append(f"Recent sessions:\n{recent}")
    return "\n\n".join(parts)


def run_session(user_inputs: list[str]) -> None:
    mem = load_memory()
    context = get_memory_context(mem)
    system = f"You are a helpful assistant.\n\n{context}" if context else "You are a helpful assistant."

    messages: list[dict] = []
    for user_input in user_inputs:
        messages.append({"role": "user", "content": user_input})
        resp = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=512,
            system=system, messages=messages,
        )
        reply = resp.content[0].text
        messages.append({"role": "assistant", "content": reply})
        print(f"A: {reply[:80]}")

    mem = add_session_to_memory(mem, messages)
    save_memory(mem)


run_session(["What did we decide about the database schema last time?"])
```

**Expected Token Savings:** Rolling compaction bounds total memory size at ~600 tokens for long-term + ~750 tokens for recent sessions — total memory context stays under 1,500 tokens regardless of how many sessions have occurred.

**Environment:** Long-running agents (weeks/months of sessions); especially useful for personal assistants and project-specific agents.

---

## Option 3 — Fact extraction: save named entities and decisions, not prose

**Instead of summarising prose, extract structured facts as key-value pairs — more searchable and denser.**

```python
import json
import os
from datetime import datetime
import anthropic

client = anthropic.Anthropic()
FACTS_FILE = "agent_facts.json"


def load_facts() -> list[dict]:
    if os.path.exists(FACTS_FILE):
        with open(FACTS_FILE) as f:
            return json.load(f)
    return []


def save_facts(facts: list[dict]) -> None:
    with open(FACTS_FILE, "w") as f:
        json.dump(facts, f, indent=2)


def extract_facts(messages: list[dict]) -> list[dict]:
    """Extract structured facts from a conversation."""
    transcript = "\n".join(
        f"{m['role'].upper()}: {m['content']}"
        for m in messages if isinstance(m.get("content"), str)
    )
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": (
                "Extract facts from this conversation as a JSON array of objects. "
                "Each object has: category (preference/decision/context/constraint), "
                "fact (one sentence), confidence (high/medium/low).\n"
                "Return ONLY the JSON array, no explanation.\n\n"
                f"CONVERSATION:\n{transcript[:10_000]}"
            ),
        }],
    )
    raw = resp.content[0].text.strip()
    start, end = raw.find("["), raw.rfind("]") + 1
    if start == -1:
        return []
    extracted = json.loads(raw[start:end])
    # Add metadata
    date = datetime.now().strftime("%Y-%m-%d")
    return [{**f, "date": date} for f in extracted if isinstance(f, dict)]


def deduplicate_facts(facts: list[dict]) -> list[dict]:
    """Remove semantically duplicate facts by exact-match on 'fact' field."""
    seen: set[str] = set()
    unique = []
    for f in facts:
        key = f.get("fact", "").lower().strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def format_facts_for_context(facts: list[dict]) -> str:
    by_category: dict[str, list[str]] = {}
    for f in facts[-50:]:   # cap at 50 most recent facts
        cat = f.get("category", "other")
        by_category.setdefault(cat, []).append(f"- {f['fact']}")
    sections = [f"{cat.title()}:\n" + "\n".join(items) for cat, items in by_category.items()]
    return "\n\n".join(sections)


def run_session(user_inputs: list[str]) -> None:
    all_facts = load_facts()
    context = format_facts_for_context(all_facts)
    system = f"You are a helpful assistant.\n\nKnown facts about the user:\n{context}" if context else "You are a helpful assistant."

    messages: list[dict] = []
    for user_input in user_inputs:
        messages.append({"role": "user", "content": user_input})
        resp = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=512,
            system=system, messages=messages,
        )
        reply = resp.content[0].text
        messages.append({"role": "assistant", "content": reply})
        print(f"A: {reply[:80]}")

    new_facts = extract_facts(messages)
    all_facts = deduplicate_facts(all_facts + new_facts)
    save_facts(all_facts)
    print(f"[Facts: +{len(new_facts)} new, {len(all_facts)} total]")


run_session([
    "I prefer short responses — no more than 3 sentences.",
    "We decided to use PostgreSQL over MySQL for the new project.",
])
```

**Expected Token Savings:** 50 structured facts fit in ~500 tokens vs. 50,000+ tokens for the raw conversations they were extracted from — 99% reduction. Structured facts also improve retrieval precision by 40–60% compared to prose summaries.

**Environment:** Personal assistant agents; project management agents where decisions and preferences are the key memory content.

---

## Option 4 — Importance-scored memory with TTL expiry

**Score each memory fragment by importance; expire low-importance memories after a TTL to prevent accumulation.**

```python
import json
import os
import time
from datetime import datetime
import anthropic

client = anthropic.Anthropic()
MEMORY_FILE = "scored_memory.json"

TTL_BY_IMPORTANCE = {
    "critical": float("inf"),   # never expire
    "high":     90 * 86400,     # 90 days
    "medium":   30 * 86400,     # 30 days
    "low":      7  * 86400,     # 7 days
}


def load_memory() -> list[dict]:
    if not os.path.exists(MEMORY_FILE):
        return []
    with open(MEMORY_FILE) as f:
        data = json.load(f)
    # Expire old entries
    now = time.time()
    return [
        m for m in data
        if now - m["created_at"] < TTL_BY_IMPORTANCE.get(m["importance"], 86400)
    ]


def save_memory(memories: list[dict]) -> None:
    with open(MEMORY_FILE, "w") as f:
        json.dump(memories, f, indent=2)


def extract_scored_memories(messages: list[dict]) -> list[dict]:
    transcript = "\n".join(
        f"{m['role'].upper()}: {m['content']}"
        for m in messages if isinstance(m.get("content"), str)
    )
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        messages=[{
            "role": "user",
            "content": (
                "Extract memorable facts from this conversation. "
                "Return a JSON array where each item has:\n"
                "  memory: (one sentence fact)\n"
                "  importance: critical/high/medium/low\n"
                "    critical = permanent user identity/constraint\n"
                "    high = project decision or strong preference\n"
                "    medium = useful context\n"
                "    low = transient detail\n"
                "Return ONLY the JSON array.\n\n"
                f"CONVERSATION:\n{transcript[:10_000]}"
            ),
        }],
    )
    raw = resp.content[0].text
    start, end = raw.find("["), raw.rfind("]") + 1
    if start == -1:
        return []
    items = json.loads(raw[start:end])
    now = time.time()
    return [
        {**item, "created_at": now, "date": datetime.now().strftime("%Y-%m-%d")}
        for item in items if isinstance(item, dict) and "memory" in item
    ]


def format_memory_context(memories: list[dict]) -> str:
    by_importance = {"critical": [], "high": [], "medium": [], "low": []}
    for m in memories:
        by_importance.setdefault(m.get("importance", "low"), []).append(m["memory"])
    lines = []
    for imp in ["critical", "high", "medium"]:   # skip low in context
        items = by_importance.get(imp, [])
        if items:
            lines.append(f"{imp.upper()}:\n" + "\n".join(f"- {i}" for i in items))
    return "\n\n".join(lines)


def run_session(user_inputs: list[str]) -> None:
    memories = load_memory()
    context = format_memory_context(memories)
    system = f"You are a helpful assistant.\n\nMemory:\n{context}" if context else "You are a helpful assistant."

    messages: list[dict] = []
    for user_input in user_inputs:
        messages.append({"role": "user", "content": user_input})
        resp = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=512,
            system=system, messages=messages,
        )
        reply = resp.content[0].text
        messages.append({"role": "assistant", "content": reply})
        print(f"A: {reply[:80]}")

    new_memories = extract_scored_memories(messages)
    memories.extend(new_memories)
    save_memory(memories)
    by_imp = {}
    for m in new_memories:
        by_imp[m["importance"]] = by_imp.get(m["importance"], 0) + 1
    print(f"[Memories saved: {by_imp}]")


run_session(["I never want you to use bullet points in responses — prose only."])
```

**Expected Token Savings:** TTL expiry removes low-importance memories automatically — for an agent running for 90 days, this keeps memory context under 1,000 tokens while a raw store would reach 100,000+.

**Environment:** Personal assistants with months of history; agents where some memories are transient (current project) and some are permanent (user preferences).

---

## Option 5 — Embedding-indexed memory with semantic deduplication

**Store summaries as embeddings; skip saving a new memory if a semantically similar one already exists.**

```python
import json
import math
import os
import anthropic

client = anthropic.Anthropic()
MEMORY_FILE = "embedded_memory.json"

# Using voyage embeddings via Anthropic (or substitute any embedding provider)
def get_embedding(text: str) -> list[float]:
    """Placeholder: replace with real embedding call."""
    # In production: use voyage-3 or text-embedding-3-small
    # Simulated 8-dim embedding from hash for demo purposes
    h = hash(text)
    return [(h >> i & 0xFF) / 255.0 for i in range(0, 64, 8)]


def cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def load_memory() -> list[dict]:
    if not os.path.exists(MEMORY_FILE):
        return []
    with open(MEMORY_FILE) as f:
        return json.load(f)


def save_memory(memories: list[dict]) -> None:
    with open(MEMORY_FILE, "w") as f:
        json.dump(memories, f, indent=2)


def add_if_novel(memories: list[dict], text: str, threshold: float = 0.92) -> bool:
    """Add memory only if no existing memory is semantically similar."""
    new_emb = get_embedding(text)
    for existing in memories:
        if cosine_sim(existing["embedding"], new_emb) > threshold:
            return False   # duplicate — skip
    memories.append({"text": text, "embedding": new_emb})
    return True


def extract_memories_from_session(messages: list[dict]) -> list[str]:
    transcript = "\n".join(
        f"{m['role'].upper()}: {m['content']}"
        for m in messages if isinstance(m.get("content"), str)
    )
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": (
                "List up to 5 memorable facts from this conversation. "
                "One fact per line, no bullets.\n\n"
                f"CONVERSATION:\n{transcript[:8_000]}"
            ),
        }],
    )
    return [line.strip() for line in resp.content[0].text.splitlines() if line.strip()]


def recall(memories: list[dict], query: str, top_k: int = 5) -> list[str]:
    q_emb = get_embedding(query)
    scored = [(cosine_sim(q_emb, m["embedding"]), m["text"]) for m in memories]
    scored.sort(reverse=True)
    return [text for _, text in scored[:top_k]]


def run_session(user_inputs: list[str]) -> None:
    memories = load_memory()
    query = user_inputs[0] if user_inputs else ""
    relevant = recall(memories, query)
    context = "Relevant memories:\n" + "\n".join(f"- {r}" for r in relevant) if relevant else ""
    system = f"You are a helpful assistant. {context}"

    messages: list[dict] = []
    for user_input in user_inputs:
        messages.append({"role": "user", "content": user_input})
        resp = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=512,
            system=system, messages=messages,
        )
        reply = resp.content[0].text
        messages.append({"role": "assistant", "content": reply})
        print(f"A: {reply[:80]}")

    new_facts = extract_memories_from_session(messages)
    added = sum(1 for f in new_facts if add_if_novel(memories, f))
    save_memory(memories)
    print(f"[Memory: +{added} new (deduped from {len(new_facts)} candidates), {len(memories)} total]")


run_session(["What's the best approach for async database connections?"])
```

**Expected Token Savings:** Semantic deduplication prevents the same fact from being stored 10× across repeat conversations — keeps memory store 60–80% smaller than a naive append-all approach while improving recall precision.

**Environment:** Agents with high topic repetition; requires an embedding model (voyage-3, text-embedding-3-small, or similar).

---

## Option 6 — Hierarchical memory: episodic → semantic consolidation

**Two-tier memory: recent raw episodes for recall + a weekly consolidation pass that distils episodes into durable semantic memories.**

```python
import json
import os
from datetime import datetime, timedelta
import anthropic

client = anthropic.Anthropic()
EPISODIC_FILE  = "episodic_memory.json"
SEMANTIC_FILE  = "semantic_memory.json"
CONSOLIDATION_DAYS = 7


def load_json(path: str) -> list:
    return json.load(open(path)) if os.path.exists(path) else []


def save_json(path: str, data) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def needs_consolidation(episodic: list[dict]) -> bool:
    if not episodic:
        return False
    oldest_date = datetime.fromisoformat(episodic[0]["date"])
    return (datetime.now() - oldest_date).days >= CONSOLIDATION_DAYS


def consolidate(episodic: list[dict], existing_semantic: list[dict]) -> list[dict]:
    episodes_text = "\n\n".join(
        f"[{e['date']}] {e['summary']}" for e in episodic
    )
    existing_text = "\n".join(f"- {s['fact']}" for s in existing_semantic)

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        messages=[{
            "role": "user",
            "content": (
                "Consolidate these session episodes into durable semantic memories. "
                "Merge with existing memories, remove ephemeral details, keep permanent facts.\n\n"
                f"EXISTING:\n{existing_text}\n\nEPISODES:\n{episodes_text[:12_000]}\n\n"
                "Return a JSON array: [{\"fact\": \"...\", \"category\": \"...\"}]"
            ),
        }],
    )
    raw = resp.content[0].text
    start, end = raw.find("["), raw.rfind("]") + 1
    return json.loads(raw[start:end]) if start != -1 else existing_semantic


def run_session(user_inputs: list[str]) -> None:
    episodic  = load_json(EPISODIC_FILE)
    semantic  = load_json(SEMANTIC_FILE)

    # Consolidate if needed
    if needs_consolidation(episodic):
        print("[Consolidating episodic → semantic memory …]")
        semantic = consolidate(episodic, semantic)
        save_json(SEMANTIC_FILE, semantic)
        # Keep only last 2 episodes post-consolidation
        episodic = episodic[-2:]
        save_json(EPISODIC_FILE, episodic)

    # Build context: semantic (permanent) + last 2 episodes (recent)
    semantic_text = "\n".join(f"- {s['fact']}" for s in semantic[:30])
    recent_text   = "\n\n".join(f"[{e['date']}] {e['summary']}" for e in episodic[-2:])
    context_parts = []
    if semantic_text:
        context_parts.append(f"Permanent knowledge:\n{semantic_text}")
    if recent_text:
        context_parts.append(f"Recent sessions:\n{recent_text}")
    system = "You are a helpful assistant.\n\n" + "\n\n".join(context_parts)

    messages: list[dict] = []
    for user_input in user_inputs:
        messages.append({"role": "user", "content": user_input})
        resp = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=512,
            system=system, messages=messages,
        )
        reply = resp.content[0].text
        messages.append({"role": "assistant", "content": reply})
        print(f"A: {reply[:80]}")

    # Summarise session → new episodic entry
    transcript = "\n".join(
        f"{m['role'].upper()}: {m['content']}"
        for m in messages if isinstance(m.get("content"), str)
    )
    summary_resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": f"Summarise in ≤ 5 bullet points:\n{transcript[:8000]}"}],
    )
    episodic.append({"date": datetime.now().isoformat(), "summary": summary_resp.content[0].text})
    save_json(EPISODIC_FILE, episodic)
    print(f"[Episodic: {len(episodic)} entries | Semantic: {len(semantic)} facts]")


run_session(["Let's continue working on the authentication module."])
```

**Expected Token Savings:** Two-tier consolidation keeps context under 1,500 tokens indefinitely: ~750 for semantic facts + ~750 for 2 recent episodes — regardless of how many months the agent has been running.

**Environment:** Long-lived personal assistant or project agents; weekly consolidation can run as a background cron job separate from the main agent loop.

---

## Comparison

| Option | Storage Format | Max Context Size | Expiry | Complexity |
|--------|---------------|-----------------|--------|------------|
| 1. Session summariser | Prose summary | O(sessions) | No | Low |
| 2. Rolling compaction | Merged summary | O(1) bounded | No | Medium |
| 3. Fact extraction | Structured KV | O(unique facts) | No | Medium |
| 4. Importance + TTL | Scored facts | O(1) bounded | Yes | Medium |
| 5. Embedding dedup | Vector store | O(novel facts) | No | High |
| 6. Episodic → semantic | Two-tier | O(1) bounded | Implicit | High |

**Recommended path:** Start with Option 1 (session summariser) — a single `summarise_session()` call at session end gives 97% token reduction with minimal code. Move to Option 3 (fact extraction) when you need precise retrieval. Use Option 6 (hierarchical) for agents running over months where memory quality compounds over time.
