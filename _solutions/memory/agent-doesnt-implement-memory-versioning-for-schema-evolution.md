---
layout: solution
title: "Agent Doesn't Implement Memory Versioning for Schema Evolution"
category: memory
description: "Version memory entries so schema changes don't corrupt existing memories, with migration paths, backward-compatible reads, and deprecation tracking."
tags: [memory, versioning, schema, migration, backward-compatibility, sqlite]
---

# Agent Doesn't Implement Memory Versioning for Schema Evolution

When the structure of memory entries changes — adding fields, renaming keys, changing types — existing stored memories break silently. An agent that loads old memories into a new schema gets `None` for required fields, crashes on missing keys, or silently uses stale data. Memory versioning ensures old entries are migrated or gracefully handled when the schema evolves.

## Option 1: Version Field on Each Memory Entry

```python
import anthropic
import json
import time
from typing import Any

client = anthropic.Anthropic()

CURRENT_VERSION = 2

# In-memory store (use SQLite or Redis in production)
memory_store: list[dict[str, Any]] = []


def save_memory(key: str, data: dict[str, Any]) -> None:
    """Save a memory entry with the current schema version."""
    entry = {
        "key": key,
        "version": CURRENT_VERSION,
        "data": data,
        "created_at": time.time(),
    }
    memory_store.append(entry)


def migrate_v1_to_v2(entry: dict) -> dict:
    """Migrate a v1 memory entry to v2 schema."""
    data = entry["data"]
    # v1 had flat 'text', v2 has 'content' + 'tags'
    return {
        "key": entry["key"],
        "version": 2,
        "data": {
            "content": data.get("text", ""),
            "tags": [],
            "importance": data.get("priority", 5),
        },
        "created_at": entry.get("created_at", time.time()),
    }


MIGRATIONS = {1: migrate_v1_to_v2}


def load_memory(key: str) -> dict[str, Any] | None:
    """Load a memory, migrating it to current version if needed."""
    entry = next((e for e in reversed(memory_store) if e["key"] == key), None)
    if entry is None:
        return None

    version = entry.get("version", 1)
    while version < CURRENT_VERSION:
        migrate_fn = MIGRATIONS.get(version)
        if not migrate_fn:
            break
        entry = migrate_fn(entry)
        version = entry["version"]

    return entry["data"]


def run_agent_with_versioned_memory(user_message: str) -> str:
    # Inject relevant memories
    context_parts = []
    for entry in memory_store[-3:]:
        data = load_memory(entry["key"])
        if data:
            context_parts.append(f"Memory: {json.dumps(data)}")

    system = "You are a helpful assistant with memory.\n\n" + "\n".join(context_parts)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


# Seed a v1 memory (old schema)
memory_store.append({
    "key": "user_pref",
    "version": 1,
    "data": {"text": "User prefers concise answers", "priority": 8},
    "created_at": time.time() - 86400,
})

# Save a v2 memory (current schema)
save_memory("recent_topic", {"content": "Discussed Python asyncio", "tags": ["python", "async"], "importance": 7})

# Load both — v1 is auto-migrated
pref = load_memory("user_pref")
topic = load_memory("recent_topic")
print(f"Migrated v1: {pref}")
print(f"Current v2: {topic}")

result = run_agent_with_versioned_memory("What did we discuss recently?")
print(f"\nAgent: {result}")

# Expected Token Savings: N/A (correctness pattern); prevents silent data loss when adding required memory fields
# Environment: Python 3.11+; store version alongside every memory entry from day one; migrations are append-only
```

## Option 2: SQLite with Schema Version Table and Column Migrations

```python
import sqlite3
import json
import time
import anthropic
from typing import Any

client = anthropic.Anthropic()
DB_PATH = ":memory:"

SCHEMA_HISTORY = [
    # (version, sql)
    (1, """
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            key TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at REAL NOT NULL
        );
    """),
    (2, """
        ALTER TABLE memories ADD COLUMN importance INTEGER DEFAULT 5;
        ALTER TABLE memories ADD COLUMN tags TEXT DEFAULT '[]';
    """),
    (3, """
        ALTER TABLE memories ADD COLUMN expires_at REAL DEFAULT NULL;
        ALTER TABLE memories ADD COLUMN access_count INTEGER DEFAULT 0;
    """),
]


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at REAL NOT NULL
        )
    """)
    conn.commit()


def get_db_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return row[0] or 0


def migrate(conn: sqlite3.Connection) -> None:
    current = get_db_version(conn)
    for version, sql in SCHEMA_HISTORY:
        if version <= current:
            continue
        print(f"Applying migration v{version}")
        for statement in sql.strip().split(";"):
            stmt = statement.strip()
            if stmt:
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise
        conn.execute("INSERT INTO schema_version VALUES (?, ?)", (version, time.time()))
        conn.commit()
        print(f"Migration v{version} applied")


def save_memory(conn: sqlite3.Connection, agent_id: str, key: str,
                content: str, importance: int = 5, tags: list[str] | None = None) -> None:
    conn.execute(
        "INSERT INTO memories (agent_id, key, content, created_at, importance, tags) VALUES (?,?,?,?,?,?)",
        (agent_id, key, content, time.time(), importance, json.dumps(tags or []))
    )
    conn.commit()


def load_memories(conn: sqlite3.Connection, agent_id: str, limit: int = 5) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT key, content, importance, tags FROM memories WHERE agent_id=? ORDER BY importance DESC, created_at DESC LIMIT ?",
        (agent_id, limit)
    ).fetchall()
    return [{"key": r[0], "content": r[1], "importance": r[2], "tags": json.loads(r[3])} for r in rows]


def run_agent(conn: sqlite3.Connection, agent_id: str, user_message: str) -> str:
    memories = load_memories(conn, agent_id)
    mem_context = "\n".join(f"- {m['content']} (importance={m['importance']})" for m in memories)
    system = f"You are a helpful assistant.\n\nKnown context:\n{mem_context}" if mem_context else "You are a helpful assistant."

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


conn = sqlite3.connect(DB_PATH)
init_db(conn)
migrate(conn)

save_memory(conn, "agent-1", "user_lang", "User prefers Python examples", importance=9, tags=["preference"])
save_memory(conn, "agent-1", "recent_task", "Helped debug asyncio deadlock", importance=7, tags=["task", "python"])

result = run_agent(conn, "agent-1", "What should I know about this user?")
print(f"\nAgent: {result}")
print(f"DB version: {get_db_version(conn)}")

# Expected Token Savings: N/A; SQLite migration log provides full audit of schema evolution history
# Environment: Python 3.11+; run migrate() at startup; ALTER TABLE is safe for adding nullable columns
```

## Option 3: Immutable Memory Entries with Version-Tagged Reads

```python
import anthropic
import json
import time
from dataclasses import dataclass, field, asdict
from typing import Any

client = anthropic.Anthropic()


@dataclass
class MemoryEntryV1:
    schema_version: int = 1
    key: str = ""
    text: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class MemoryEntryV2:
    schema_version: int = 2
    key: str = ""
    content: str = ""
    summary: str = ""
    importance: int = 5
    tags: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)


@dataclass
class MemoryEntryV3:
    schema_version: int = 3
    key: str = ""
    content: str = ""
    summary: str = ""
    importance: int = 5
    tags: list[str] = field(default_factory=list)
    expires_at: float | None = None
    source_agent: str = ""
    created_at: float = field(default_factory=time.time)


def to_v3(raw: dict) -> MemoryEntryV3:
    """Normalize any version entry to V3."""
    v = raw.get("schema_version", 1)
    if v == 3:
        return MemoryEntryV3(**{k: raw[k] for k in MemoryEntryV3.__dataclass_fields__ if k in raw})
    if v == 2:
        return MemoryEntryV3(
            key=raw.get("key", ""),
            content=raw.get("content", ""),
            summary=raw.get("summary", ""),
            importance=raw.get("importance", 5),
            tags=raw.get("tags", []),
            source_agent="migrated_from_v2",
        )
    # v1
    return MemoryEntryV3(
        key=raw.get("key", ""),
        content=raw.get("text", ""),
        summary="",
        importance=5,
        tags=[],
        source_agent="migrated_from_v1",
    )


# Simulate a mixed-version memory store
raw_store: list[dict] = [
    {"schema_version": 1, "key": "pref_1", "text": "User likes concise replies", "created_at": time.time() - 7200},
    {"schema_version": 2, "key": "task_1", "content": "Debugged a race condition", "summary": "Race condition fix", "importance": 8, "tags": ["debug"], "created_at": time.time() - 3600},
]


def load_all_as_v3() -> list[MemoryEntryV3]:
    return [to_v3(raw) for raw in raw_store]


def save_v3(entry: MemoryEntryV3) -> None:
    raw_store.append(asdict(entry))


def run_agent(user_message: str) -> str:
    memories = load_all_as_v3()
    context = "\n".join(
        f"[{m.key}] {m.content} (importance={m.importance}, tags={m.tags})"
        for m in sorted(memories, key=lambda x: -x.importance)
    )
    system = f"Assistant with memory:\n{context}"
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )

    # Save response as new V3 memory
    save_v3(MemoryEntryV3(
        key=f"response_{int(time.time())}",
        content=f"User asked: {user_message[:80]}",
        summary="Recent interaction",
        importance=4,
        source_agent="agent-main",
    ))
    return response.content[0].text


result = run_agent("Summarize what you know about me.")
print(f"Agent: {result}")
print(f"\nAll memories normalized to V3:")
for m in load_all_as_v3():
    print(f"  [{m.key}] v{m.schema_version}->3 | source={m.source_agent} | content={m.content[:60]}")

# Expected Token Savings: N/A; immutable entries + normalization-on-read avoids destructive in-place migrations
# Environment: Python 3.11+; add new dataclass version; update to_v3() — never modify existing version classes
```

## Option 4: Content-Addressed Memory with Hash-Based Version Detection

```python
import anthropic
import json
import hashlib
import time
import sqlite3
from typing import Any

client = anthropic.Anthropic()
DB_PATH = ":memory:"

FIELD_SCHEMAS = {
    "v1": {"required": ["text"], "optional": []},
    "v2": {"required": ["content", "importance"], "optional": ["tags"]},
    "v3": {"required": ["content", "importance", "tags", "expires_at"], "optional": ["source"]},
}


def detect_schema_version(data: dict) -> str:
    """Infer schema version from field presence."""
    if "expires_at" in data and "tags" in data:
        return "v3"
    if "importance" in data:
        return "v2"
    return "v1"


def content_hash(data: dict) -> str:
    canonical = json.dumps(data, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            hash TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            key TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            normalized_json TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    conn.commit()


def normalize(data: dict, from_version: str) -> dict:
    """Normalize any version to v3 shape."""
    if from_version == "v3":
        return data
    if from_version == "v2":
        return {
            "content": data.get("content", ""),
            "importance": data.get("importance", 5),
            "tags": data.get("tags", []),
            "expires_at": None,
            "source": data.get("source", ""),
        }
    # v1
    return {
        "content": data.get("text", ""),
        "importance": 5,
        "tags": [],
        "expires_at": None,
        "source": "migrated_v1",
    }


def store_memory(conn: sqlite3.Connection, agent_id: str, key: str, data: dict) -> str:
    version = detect_schema_version(data)
    normalized = normalize(data, version)
    h = content_hash(data)
    conn.execute(
        "INSERT OR IGNORE INTO memories VALUES (?,?,?,?,?,?,?)",
        (h, agent_id, key, version, json.dumps(data), json.dumps(normalized), time.time())
    )
    conn.commit()
    return h


def load_normalized(conn: sqlite3.Connection, agent_id: str, limit: int = 5) -> list[dict]:
    rows = conn.execute(
        "SELECT key, normalized_json, schema_version FROM memories WHERE agent_id=? ORDER BY created_at DESC LIMIT ?",
        (agent_id, limit)
    ).fetchall()
    return [{"key": r[0], **json.loads(r[1]), "original_schema": r[2]} for r in rows]


def run_agent(conn: sqlite3.Connection, agent_id: str, question: str) -> str:
    memories = load_normalized(conn, agent_id)
    context = "\n".join(
        f"- {m['content']} [schema={m['original_schema']}, importance={m['importance']}]"
        for m in memories
    )
    system = f"You have the following memory context:\n{context}" if context else "No prior context."
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text


conn = sqlite3.connect(DB_PATH)
init_db(conn)

# Store mixed-version entries
store_memory(conn, "agent-1", "old_pref", {"text": "User is a Python developer"})
store_memory(conn, "agent-1", "mid_pref", {"content": "User works on ML", "importance": 8})
store_memory(conn, "agent-1", "new_pref", {"content": "User prefers async code", "importance": 9, "tags": ["style"], "expires_at": None})

normalized = load_normalized(conn, "agent-1")
print("Normalized memories:")
for m in normalized:
    print(f"  [{m['key']}] original_schema={m['original_schema']} content={m['content']}")

result = run_agent(conn, "agent-1", "What do you know about this user?")
print(f"\nAgent: {result}")

# Expected Token Savings: N/A; content hash deduplicates identical memories stored multiple times across schema versions
# Environment: Python 3.11+; hash-based storage makes re-migration idempotent — safe to rerun detect+normalize
```

## Option 5: Async Memory Migrator with Background Backfill

```python
import asyncio
import sqlite3
import json
import time
import anthropic

client = anthropic.AsyncAnthropic()
DB_PATH = ":memory:"

TARGET_VERSION = 3


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            key TEXT NOT NULL,
            schema_version INTEGER NOT NULL DEFAULT 1,
            data TEXT NOT NULL,
            migrated_at REAL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS migration_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id INTEGER NOT NULL,
            from_version INTEGER,
            to_version INTEGER,
            migrated_at REAL NOT NULL,
            success INTEGER NOT NULL
        );
    """)
    conn.commit()


def migrate_entry(data: dict, from_v: int, to_v: int) -> dict:
    result = dict(data)
    if from_v < 2 <= to_v:
        result["importance"] = result.pop("priority", 5)
        result["content"] = result.pop("text", "")
    if from_v < 3 <= to_v:
        result.setdefault("tags", [])
        result.setdefault("expires_at", None)
    return result


async def backfill_migrations(conn: sqlite3.Connection) -> int:
    """Background task: migrate all entries below TARGET_VERSION."""
    stale = conn.execute(
        "SELECT id, schema_version, data FROM memories WHERE schema_version < ?",
        (TARGET_VERSION,)
    ).fetchall()

    migrated = 0
    for mem_id, version, raw in stale:
        data = json.loads(raw)
        try:
            new_data = migrate_entry(data, version, TARGET_VERSION)
            conn.execute(
                "UPDATE memories SET data=?, schema_version=?, migrated_at=? WHERE id=?",
                (json.dumps(new_data), TARGET_VERSION, time.time(), mem_id)
            )
            conn.execute(
                "INSERT INTO migration_log VALUES (NULL,?,?,?,?,1)",
                (mem_id, version, TARGET_VERSION, time.time())
            )
            migrated += 1
        except Exception as e:
            conn.execute(
                "INSERT INTO migration_log VALUES (NULL,?,?,?,?,0)",
                (mem_id, version, TARGET_VERSION, time.time())
            )
        await asyncio.sleep(0)  # yield to event loop

    conn.commit()
    return migrated


async def run_agent(conn: sqlite3.Connection, agent_id: str, question: str) -> str:
    rows = conn.execute(
        "SELECT key, data FROM memories WHERE agent_id=? ORDER BY created_at DESC LIMIT 5",
        (agent_id,)
    ).fetchall()

    context = []
    for key, raw in rows:
        data = json.loads(raw)
        # Graceful fallback for un-migrated entries
        content = data.get("content") or data.get("text") or str(data)
        context.append(f"- {content}")

    system = "Memory context:\n" + "\n".join(context) if context else "No context."
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text


async def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    # Seed old-schema entries
    conn.executemany(
        "INSERT INTO memories (agent_id, key, schema_version, data, created_at) VALUES (?,?,?,?,?)",
        [
            ("a1", "k1", 1, json.dumps({"text": "User is a Go developer", "priority": 7}), time.time() - 100),
            ("a1", "k2", 2, json.dumps({"content": "User uses Linux", "importance": 6}), time.time() - 50),
            ("a1", "k3", 3, json.dumps({"content": "User prefers dark mode", "importance": 4, "tags": ["ui"], "expires_at": None}), time.time()),
        ]
    )
    conn.commit()

    # Run agent first (with graceful fallback for old schemas)
    answer = await run_agent(conn, "a1", "Describe this user.")
    print(f"Before migration:\n{answer}\n")

    # Background migration
    migrated = await backfill_migrations(conn)
    print(f"Migrated {migrated} entries to v{TARGET_VERSION}")

    # Run again (now all entries are v3)
    answer2 = await run_agent(conn, "a1", "Describe this user.")
    print(f"\nAfter migration:\n{answer2}")


asyncio.run(main())

# Expected Token Savings: N/A; background backfill avoids blocking agent startup on large memory stores
# Environment: Python 3.11+; run backfill in asyncio.create_task() so it doesn't delay first request
```

## Option 6: Memory Schema Registry with Deprecation Lifecycle

```python
import anthropic
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

client = anthropic.Anthropic()


@dataclass
class MemorySchema:
    version: int
    fields: dict[str, type]
    required: list[str]
    deprecated_at: float | None = None
    sunset_at: float | None = None
    upgrade: Callable[[dict], dict] | None = None


SCHEMA_REGISTRY: dict[int, MemorySchema] = {
    1: MemorySchema(
        version=1,
        fields={"text": str, "priority": int},
        required=["text"],
        deprecated_at=time.time() - 86400,  # deprecated 1 day ago
        sunset_at=time.time() + 86400 * 30, # sunset in 30 days
        upgrade=lambda d: {"content": d.get("text", ""), "importance": d.get("priority", 5), "tags": []},
    ),
    2: MemorySchema(
        version=2,
        fields={"content": str, "importance": int, "tags": list},
        required=["content", "importance"],
        upgrade=lambda d: {**d, "tags": d.get("tags", []), "source": "upgraded_from_v2"},
    ),
    3: MemorySchema(
        version=3,
        fields={"content": str, "importance": int, "tags": list, "source": str},
        required=["content", "importance", "tags", "source"],
    ),
}

CURRENT_VERSION = 3
memory_store: list[dict[str, Any]] = []


def check_lifecycle(schema: MemorySchema) -> None:
    now = time.time()
    if schema.sunset_at and now > schema.sunset_at:
        raise RuntimeError(f"Memory schema v{schema.version} has been sunset. Cannot read or write.")
    if schema.deprecated_at and now > schema.deprecated_at:
        print(f"WARNING: Memory schema v{schema.version} is deprecated. Migrate to v{CURRENT_VERSION}.")


def upgrade_to_current(data: dict, from_version: int) -> dict:
    """Walk upgrade chain from from_version to CURRENT_VERSION."""
    current_data = dict(data)
    for v in range(from_version, CURRENT_VERSION):
        schema = SCHEMA_REGISTRY.get(v)
        if schema and schema.upgrade:
            current_data = schema.upgrade(current_data)
    return current_data


def save(key: str, data: dict) -> None:
    schema = SCHEMA_REGISTRY[CURRENT_VERSION]
    missing = [f for f in schema.required if f not in data]
    if missing:
        raise ValueError(f"Missing required fields for v{CURRENT_VERSION}: {missing}")
    memory_store.append({"key": key, "version": CURRENT_VERSION, "data": data, "ts": time.time()})


def load(key: str) -> dict[str, Any] | None:
    entry = next((e for e in reversed(memory_store) if e["key"] == key), None)
    if not entry:
        return None
    version = entry.get("version", 1)
    schema = SCHEMA_REGISTRY.get(version)
    if schema:
        check_lifecycle(schema)
    return upgrade_to_current(entry["data"], version)


def run_agent(question: str) -> str:
    context = []
    for entry in memory_store:
        data = load(entry["key"])
        if data:
            context.append(f"- {data.get('content', '')} (importance={data.get('importance', 0)})")

    system = "Memory:\n" + "\n".join(context) if context else "No memory."
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text


# Simulate old v1 entry in store
memory_store.append({
    "key": "old_pref",
    "version": 1,
    "data": {"text": "User likes brevity", "priority": 9},
    "ts": time.time() - 3600,
})

# Save a current v3 entry
save("new_pref", {"content": "User works in fintech", "importance": 8, "tags": ["domain"], "source": "onboarding"})

# Load both — v1 triggers deprecation warning and auto-upgrades
print("Loading memories:")
for entry in memory_store:
    data = load(entry["key"])
    print(f"  [{entry['key']}] v{entry['version']} -> current: {data}")

result = run_agent("Tell me about this user.")
print(f"\nAgent: {result}")

# Expected Token Savings: N/A; lifecycle tracking surfaces migrations that have been pending too long before sunset
# Environment: Python 3.11+; set sunset_at = deprecated_at + 60-90 days; enforce sunset in load() to force cleanup
```

## Comparison

| Option | Migration Strategy | Backward Compat Read | Audit Trail | Background | Best For |
|--------|-------------------|---------------------|-------------|------------|----------|
| 1. Version Field | Per-entry version + chain walk | Yes | No | No | Simple single-process agents |
| 2. SQLite ALTER TABLE | DB-level schema + version table | Yes (graceful) | Yes | No | Persistent production stores |
| 3. Immutable + Normalize | Read-time normalization | Yes | No | No | Append-only stores |
| 4. Content-Addressed | Hash + normalize on write | Yes | Implicit | No | Dedup + migration combined |
| 5. Async Backfill | Background async migration | Yes (fallback) | Yes | Yes | Large stores, non-blocking startup |
| 6. Schema Registry | Lifecycle-aware registry | Yes + warnings | Via deprecation | No | Production with sunset enforcement |
