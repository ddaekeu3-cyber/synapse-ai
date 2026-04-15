---
layout: solution
title: "Agent Writes Duplicate Memories on Every Session"
category: memory
description: "Agent appends new memory entries each session without checking existing ones, accumulating thousands of duplicate facts that slow retrieval and inflate storage costs."
tags: [memory, deduplication, session, storage, retrieval]
---

## Symptom

After a few dozen sessions the memory store contains thousands of entries like `"User prefers concise responses"` repeated verbatim. Retrieval calls return the same fact twenty times, the context window fills with duplicates before useful memories appear, and embedding search becomes slower as the index grows without bound. Token costs for memory-augmented calls grow linearly with session count even though no new information is being added.

## Root Cause

The agent always calls `memory.add(fact)` without first asking whether a semantically equivalent entry already exists. Each session sees the same recurring facts (user preferences, project context, recurring instructions) and inserts them fresh. Without a deduplication gate the memory store is append-only: write path is O(1) but read quality degrades with every duplicate insertion.

## Fix

### Option 1 — Exact-match dedup before insert

```python
import json
import os
import anthropic

client = anthropic.Anthropic()

MEMORY_FILE = "/tmp/agent_memory.json"

def load_memories() -> list[str]:
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE) as f:
            return json.load(f)
    return []

def save_memories(memories: list[str]) -> None:
    tmp = MEMORY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(memories, f, indent=2)
    os.replace(tmp, MEMORY_FILE)

def add_memory(fact: str) -> bool:
    """Returns True if fact was added, False if duplicate."""
    memories = load_memories()
    normalized = fact.strip().lower()
    for existing in memories:
        if existing.strip().lower() == normalized:
            print(f"[memory] duplicate skipped: {fact[:60]}")
            return False
    memories.append(fact.strip())
    save_memories(memories)
    print(f"[memory] stored: {fact[:60]}")
    return True

def extract_and_store_facts(conversation: str) -> None:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                f"Extract key facts from this conversation as a JSON list of strings. "
                f"Be concise; one sentence per fact.\n\n{conversation}"
            ),
        }],
    )
    text = response.content[0].text
    try:
        facts = json.loads(text)
    except json.JSONDecodeError:
        facts = [line.strip("- ").strip() for line in text.splitlines() if line.strip()]

    added = sum(1 for f in facts if add_memory(f))
    print(f"[memory] {added}/{len(facts)} new facts stored (exact-match dedup)")

extract_and_store_facts("User said they prefer Python. User works at Acme Corp. User prefers Python.")
```

**Expected Token Savings:** Prevents re-embedding and re-retrieving identical facts on every subsequent session; memory retrieval calls stay O(1) per unique fact.
**Environment:** File-backed or lightweight memory stores; fast path with zero extra API calls.

---

### Option 2 — Embedding similarity dedup (cosine threshold)

```python
import json
import os
import math
import anthropic

client = anthropic.Anthropic()

MEMORY_FILE = "/tmp/agent_memory_embed.json"
SIMILARITY_THRESHOLD = 0.95  # facts above this are considered duplicates

def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0

def embed(text: str) -> list[float]:
    # Using a lightweight local embedding; swap for your embedding provider
    # Here we simulate with a Claude call that returns a vector summary hash
    # In production use sentence-transformers or OpenAI text-embedding-3-small
    import hashlib
    digest = hashlib.sha256(text.lower().encode()).digest()
    # 8-dimensional mock vector — replace with real embeddings in production
    return [((b / 255) * 2 - 1) for b in digest[:8]]

def load_memories() -> list[dict]:
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE) as f:
            return json.load(f)
    return []

def save_memories(memories: list[dict]) -> None:
    tmp = MEMORY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(memories, f, indent=2)
    os.replace(tmp, MEMORY_FILE)

def add_memory_with_embed(fact: str) -> bool:
    memories = load_memories()
    vec = embed(fact)
    for entry in memories:
        sim = cosine_similarity(vec, entry["vec"])
        if sim >= SIMILARITY_THRESHOLD:
            print(f"[memory] near-duplicate (sim={sim:.3f}), skipping: {fact[:60]}")
            return False
    memories.append({"text": fact, "vec": vec})
    save_memories(memories)
    print(f"[memory] stored new fact: {fact[:60]}")
    return True

def run_session(facts: list[str]) -> None:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": "What do you know about me?"}],
    )
    print(f"[agent] {response.content[0].text[:80]}")
    added = sum(1 for f in facts if add_memory_with_embed(f))
    print(f"[session] {added}/{len(facts)} facts were new")

# Session 1 and 2 provide the same facts — only session 1 should add them
run_session(["User prefers concise answers", "User works in fintech"])
run_session(["User prefers concise answers", "User is a senior engineer"])
```

**Expected Token Savings:** Catches paraphrased duplicates exact-match misses; avoids embedding redundant facts into prompts which consume 20–200 tokens each per session.
**Environment:** Vector-backed memory stores (Chroma, Pinecone, pgvector); medium-scale agents with thousands of potential facts.

---

### Option 3 — Hash-based dedup with content normalisation

```python
import hashlib
import json
import os
import re
import anthropic

client = anthropic.Anthropic()

MEMORY_FILE = "/tmp/agent_memory_hash.json"

def normalise(text: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation for stable hashing."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text

def content_hash(text: str) -> str:
    return hashlib.md5(normalise(text).encode()).hexdigest()

def load_store() -> dict:
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE) as f:
            return json.load(f)
    return {"hashes": {}, "facts": []}

def save_store(store: dict) -> None:
    tmp = MEMORY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(store, f, indent=2)
    os.replace(tmp, MEMORY_FILE)

def add_fact(fact: str, source: str = "session") -> bool:
    store = load_store()
    h = content_hash(fact)
    if h in store["hashes"]:
        # Bump access count for existing fact
        store["hashes"][h]["seen"] += 1
        save_store(store)
        print(f"[memory] hash hit (seen {store['hashes'][h]['seen']}x): {fact[:50]}")
        return False
    store["hashes"][h] = {"seen": 1, "source": source}
    store["facts"].append({"text": fact, "hash": h})
    save_store(store)
    print(f"[memory] new fact stored [hash={h[:8]}]: {fact[:50]}")
    return True

def summarise_and_store(conversation_turns: list[dict]) -> None:
    prompt = "\n".join(f"{t['role']}: {t['content']}" for t in conversation_turns)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"List key facts to remember from this conversation as a JSON array:\n\n{prompt}",
        }],
    )
    try:
        facts = json.loads(response.content[0].text)
    except json.JSONDecodeError:
        facts = []
    added = sum(1 for f in facts if add_fact(f))
    print(f"[memory] {added}/{len(facts)} unique facts persisted this session")

turns = [
    {"role": "user",    "content": "I prefer dark mode in all my tools."},
    {"role": "assistant","content": "Got it."},
    {"role": "user",    "content": "I prefer dark mode."},  # near-duplicate
]
summarise_and_store(turns)
summarise_and_store(turns)  # second session — should add 0 facts
```

**Expected Token Savings:** Zero-cost dedup (pure hashing, no extra API calls); seen-count telemetry helps identify which facts are worth keeping long-term.
**Environment:** Any memory backend; useful when embedding infrastructure is unavailable or too costly.

---

### Option 4 — Age-based replacement: overwrite stale facts

```python
import json
import os
import time
import anthropic

client = anthropic.Anthropic()

MEMORY_FILE = "/tmp/agent_memory_ttl.json"
MAX_AGE_SECONDS = 7 * 24 * 3600  # facts older than 1 week are candidates for replacement

def load_store() -> list[dict]:
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE) as f:
            return json.load(f)
    return []

def save_store(store: list[dict]) -> None:
    tmp = MEMORY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(store, f, indent=2)
    os.replace(tmp, MEMORY_FILE)

def upsert_fact(fact: str, category: str) -> None:
    """Replace an existing same-category fact if stale, otherwise skip if fresh."""
    store = load_store()
    now = time.time()

    # Find existing fact in same category
    for entry in store:
        if entry.get("category") == category:
            age = now - entry["timestamp"]
            if age < MAX_AGE_SECONDS:
                print(f"[memory] fresh fact exists ({age/3600:.1f}h old), skipping: {fact[:50]}")
                return
            else:
                print(f"[memory] replacing stale fact ({age/3600:.1f}h old): {fact[:50]}")
                entry["text"] = fact
                entry["timestamp"] = now
                save_store(store)
                return

    # No existing fact in this category — add new
    store.append({"category": category, "text": fact, "timestamp": now})
    save_store(store)
    print(f"[memory] new fact added [{category}]: {fact[:50]}")

def extract_categorised_facts(session_text: str) -> None:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                "Extract facts as JSON objects with 'fact' and 'category' keys "
                "(categories: preference, identity, project, instruction).\n\n"
                f"Text:\n{session_text}"
            ),
        }],
    )
    try:
        facts = json.loads(response.content[0].text)
    except json.JSONDecodeError:
        facts = []
    for item in facts:
        upsert_fact(item.get("fact", ""), item.get("category", "general"))

extract_categorised_facts("User likes Python and works on a trading platform.")
extract_categorised_facts("User likes Python and works on a trading platform.")  # duplicate session
```

**Expected Token Savings:** Bounds memory size to one entry per category; stale overwrite keeps context fresh without unbounded growth.
**Environment:** Personal assistant agents where user preferences evolve over weeks; single-user deployments.

---

### Option 5 — LLM-powered merge: consolidate contradictory facts

```python
import json
import os
import anthropic

client = anthropic.Anthropic()

MEMORY_FILE = "/tmp/agent_memory_merge.json"

def load_memories() -> list[str]:
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE) as f:
            return json.load(f)
    return []

def save_memories(memories: list[str]) -> None:
    tmp = MEMORY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(memories, f, indent=2)
    os.replace(tmp, MEMORY_FILE)

def merge_facts(existing: list[str], candidates: list[str]) -> list[str]:
    """Ask Claude to deduplicate and merge two sets of facts."""
    if not existing:
        return candidates
    prompt = (
        "You are a memory manager. Given existing facts and new candidate facts, "
        "return a merged JSON list that:\n"
        "1. Removes exact and semantic duplicates (keep the most specific version)\n"
        "2. Updates contradictions with the newer fact\n"
        "3. Adds genuinely new facts\n\n"
        f"Existing:\n{json.dumps(existing, indent=2)}\n\n"
        f"Candidates:\n{json.dumps(candidates, indent=2)}\n\n"
        "Return only the merged JSON array."
    )
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        return json.loads(response.content[0].text)
    except json.JSONDecodeError:
        # Fallback: return existing with candidates appended (no merge)
        return existing + candidates

def store_session_facts(new_facts: list[str]) -> None:
    existing = load_memories()
    before_count = len(existing)
    merged = merge_facts(existing, new_facts)
    save_memories(merged)
    delta = len(merged) - before_count
    print(f"[memory] {before_count} → {len(merged)} facts ({delta:+d} net change)")

# Session 1
store_session_facts([
    "User prefers Python over JavaScript",
    "User works at Acme Corp",
    "User likes dark mode",
])
# Session 2 — same facts plus one update
store_session_facts([
    "User prefers Python",
    "User now works at BetaCo",  # contradicts session 1
    "User likes dark mode",
    "User has 10 years of experience",
])
```

**Expected Token Savings:** One merge call per session instead of N retrieval calls for N duplicates; merged memories are 40–70% smaller than raw accumulated lists.
**Environment:** Long-running personal assistants where facts evolve and contradict each other; worth the small merge-call cost at session boundaries.

---

### Option 6 — Database UPSERT pattern with conflict resolution

```python
import asyncio
import time
import anthropic

# asyncpg is the async PostgreSQL driver; install: pip install asyncpg
try:
    import asyncpg
    HAS_ASYNCPG = True
except ImportError:
    HAS_ASYNCPG = False

client = anthropic.AsyncAnthropic()

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS agent_memories (
    id          SERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL,
    category    TEXT NOT NULL,
    fact        TEXT NOT NULL,
    confidence  FLOAT DEFAULT 1.0,
    last_seen   BIGINT NOT NULL,
    seen_count  INT DEFAULT 1,
    UNIQUE (user_id, category)   -- one canonical fact per user+category
);
"""

UPSERT_SQL = """
INSERT INTO agent_memories (user_id, category, fact, last_seen, seen_count)
VALUES ($1, $2, $3, $4, 1)
ON CONFLICT (user_id, category) DO UPDATE
    SET fact       = EXCLUDED.fact,
        last_seen  = EXCLUDED.last_seen,
        seen_count = agent_memories.seen_count + 1
RETURNING id, seen_count;
"""

async def upsert_memory(pool, user_id: str, category: str, fact: str) -> dict:
    row = await pool.fetchrow(UPSERT_SQL, user_id, category, fact, int(time.time()))
    return {"id": row["id"], "seen_count": row["seen_count"]}

async def extract_and_upsert(pool, user_id: str, conversation: str) -> None:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                "Extract facts as JSON list of {category, fact} objects "
                "(categories: preference, identity, project, instruction):\n\n"
                f"{conversation}"
            ),
        }],
    )
    import json
    try:
        facts = json.loads(response.content[0].text)
    except json.JSONDecodeError:
        facts = []

    results = await asyncio.gather(*[
        upsert_memory(pool, user_id, item["category"], item["fact"])
        for item in facts if "category" in item and "fact" in item
    ])
    new_count = sum(1 for r in results if r["seen_count"] == 1)
    dup_count = len(results) - new_count
    print(f"[memory] upserted {len(results)} facts: {new_count} new, {dup_count} updated")

async def demo():
    if not HAS_ASYNCPG:
        print("[demo] asyncpg not installed — showing SQL patterns only")
        print(f"UPSERT SQL:\n{UPSERT_SQL}")
        return

    pool = await asyncpg.create_pool("postgresql://localhost/demo", min_size=2, max_size=5)
    async with pool.acquire() as conn:
        await conn.execute(CREATE_TABLE)

    conversation = "Alice prefers TypeScript. She works on a payment gateway at FinCorp."
    await extract_and_upsert(pool, "user:alice", conversation)
    await extract_and_upsert(pool, "user:alice", conversation)  # duplicate session
    await pool.close()

asyncio.run(demo())
```

**Expected Token Savings:** Database-level dedup requires zero extra API calls; UNIQUE constraint makes duplicates physically impossible regardless of concurrency or crash-recovery scenarios.
**Environment:** Multi-session production agents backed by PostgreSQL; multi-user SaaS products where per-user memory isolation and seen_count analytics are needed.

---

## Comparison

| Option | Mechanism | Extra API Calls | Handles Paraphrases | Handles Contradictions | Best For |
|---|---|---|---|---|---|
| 1. Exact-match | String equality | 0 | No | No | Minimal setup, controlled fact strings |
| 2. Embedding similarity | Cosine threshold | 0 (local embed) | Yes | No | Semantic dedup, vector stores |
| 3. Hash-based | MD5 of normalised text | 0 | Partial | No | Fast, no-infra dedup with seen-count |
| 4. Age-based replacement | Timestamp + category | 1 (extract) | Yes (by category) | Yes (newer wins) | Evolving preferences, single-user |
| 5. LLM merge | Claude merge call | 1 per session | Yes | Yes | Complex evolving facts, accuracy priority |
| 6. Database UPSERT | SQL ON CONFLICT | 1 (extract) | No | Yes (newer wins) | Production multi-user systems |
