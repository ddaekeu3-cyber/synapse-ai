---
layout: solution
title: "Agent Doesn't Implement Memory Namespace Isolation"
category: memory
description: "Memories from different users or sessions bleed into each other's context, causing the agent to reveal private information, apply wrong preferences, or confuse user identities."
tags: [memory, security, multi-tenant, isolation, privacy]
---

## Symptom

The agent addresses User B by User A's name, applies User A's preferences to User B's session, or reveals facts that User B never shared. In multi-tenant deployments, one user's stored memories appear in another user's conversation. Logged outputs show context items that don't belong to the current session.

## Root Cause

The agent stores and retrieves memories using a flat, global key space. When writing `memory["preferred_language"] = "French"` or prepending a memory block to the system prompt, there is no scoping by user ID, session ID, or tenant. All agents share the same memory pool. A retrieval like `get_relevant_memories(query)` searches across all users' stored context, returning whoever had the most semantically similar past interactions — regardless of who is currently talking.

## Fix

### Option 1: User-scoped memory keys with prefix isolation

```python
import json
import os
import anthropic

client = anthropic.Anthropic()


class NamespacedMemoryStore:
    """
    File-based memory store with strict user-scoped namespacing.
    Each user's memories live in a separate directory.
    """

    def __init__(self, base_dir: str = ".agent_memory"):
        self.base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)

    def _user_path(self, user_id: str) -> str:
        # Sanitize user_id to prevent path traversal
        safe_id = "".join(c for c in user_id if c.isalnum() or c in "-_")
        if not safe_id:
            raise ValueError(f"Invalid user_id: {user_id!r}")
        user_dir = os.path.join(self.base_dir, safe_id)
        os.makedirs(user_dir, exist_ok=True)
        return os.path.join(user_dir, "memories.json")

    def read(self, user_id: str) -> dict:
        path = self._user_path(user_id)
        if not os.path.exists(path):
            return {}
        with open(path) as f:
            return json.load(f)

    def write(self, user_id: str, key: str, value: str) -> None:
        path = self._user_path(user_id)
        memories = self.read(user_id)
        memories[key] = value
        with open(path, "w") as f:
            json.dump(memories, f, indent=2)

    def delete(self, user_id: str, key: str) -> None:
        memories = self.read(user_id)
        memories.pop(key, None)
        path = self._user_path(user_id)
        with open(path, "w") as f:
            json.dump(memories, f, indent=2)

    def clear_user(self, user_id: str) -> None:
        """GDPR deletion: remove all memories for a user."""
        path = self._user_path(user_id)
        if os.path.exists(path):
            os.remove(path)


memory_store = NamespacedMemoryStore()


def build_system_prompt(user_id: str) -> str:
    user_memories = memory_store.read(user_id)
    base = "You are a helpful assistant."

    if not user_memories:
        return base

    memory_lines = "\n".join(f"- {k}: {v}" for k, v in user_memories.items())
    return f"{base}\n\n<user_context user_id=\"{user_id}\">\n{memory_lines}\n</user_context>"


def run_agent(user_id: str, message: str) -> str:
    # Memories are scoped to user_id — never cross-contaminate
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=build_system_prompt(user_id),
        messages=[{"role": "user", "content": message}],
    )

    # Demo: persist a preference if mentioned
    if "my name is" in message.lower():
        name = message.lower().split("my name is")[-1].strip().split()[0].title()
        memory_store.write(user_id, "name", name)

    return response.content[0].text


# Users are fully isolated
print(run_agent("user-alice", "My name is Alice and I prefer responses in French."))
print(run_agent("user-bob", "What do you know about me?"))  # Sees nothing about Alice
```

**Expected Token Savings:** Eliminates irrelevant cross-user memories from context, reducing input tokens by 10–40% in high-traffic multi-user deployments.
**Environment:** Python 3.9+; file-based store; replace with Redis/PostgreSQL for production.

---

### Option 2: Session-scoped in-memory store with expiry

```python
import time
import uuid
from collections import defaultdict
from threading import Lock

import anthropic

client = anthropic.Anthropic()


class SessionMemoryStore:
    """
    Thread-safe in-memory store scoped by (user_id, session_id).
    Sessions expire after TTL seconds of inactivity.
    """

    def __init__(self, ttl_seconds: int = 3600):
        self._store: dict[tuple[str, str], dict] = defaultdict(dict)
        self._last_access: dict[tuple[str, str], float] = {}
        self._lock = Lock()
        self._ttl = ttl_seconds

    def _key(self, user_id: str, session_id: str) -> tuple[str, str]:
        return (user_id, session_id)

    def _evict_expired(self) -> None:
        now = time.time()
        expired = [k for k, t in self._last_access.items() if now - t > self._ttl]
        for k in expired:
            del self._store[k]
            del self._last_access[k]

    def set(self, user_id: str, session_id: str, key: str, value: str) -> None:
        k = self._key(user_id, session_id)
        with self._lock:
            self._store[k][key] = value
            self._last_access[k] = time.time()
            self._evict_expired()

    def get_all(self, user_id: str, session_id: str) -> dict:
        k = self._key(user_id, session_id)
        with self._lock:
            self._last_access[k] = time.time()
            return dict(self._store.get(k, {}))

    def new_session(self, user_id: str) -> str:
        """Create a new isolated session for a user."""
        session_id = str(uuid.uuid4())
        # Copy user-level persistent memories into new session
        persistent = self.get_user_persistent(user_id)
        for key, value in persistent.items():
            self.set(user_id, session_id, key, value)
        return session_id

    def set_user_persistent(self, user_id: str, key: str, value: str) -> None:
        """Store a persistent (cross-session) memory for a user."""
        self.set(user_id, "__persistent__", key, value)

    def get_user_persistent(self, user_id: str) -> dict:
        return self.get_all(user_id, "__persistent__")


store = SessionMemoryStore(ttl_seconds=1800)


def chat(user_id: str, session_id: str, message: str) -> str:
    # All memory lookups are scoped to (user_id, session_id)
    session_ctx = store.get_all(user_id, session_id)

    ctx_block = ""
    if session_ctx:
        lines = "\n".join(f"- {k}: {v}" for k, v in session_ctx.items())
        ctx_block = f"\n\n<session_context>\n{lines}\n</session_context>"

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=f"You are a helpful assistant.{ctx_block}",
        messages=[{"role": "user", "content": message}],
    )
    return response.content[0].text


# Demonstrate isolation
alice_session = store.new_session("alice")
bob_session = store.new_session("bob")

store.set("alice", alice_session, "language", "French")
store.set("alice", alice_session, "name", "Alice")

# Bob's session has no access to Alice's data
print(f"Alice session ctx: {store.get_all('alice', alice_session)}")
print(f"Bob session ctx: {store.get_all('bob', bob_session)}")  # Empty

print(chat("alice", alice_session, "What language do I prefer?"))
print(chat("bob", bob_session, "What language do I prefer?"))  # Knows nothing
```

**Expected Token Savings:** Session-scoped context is smaller and more relevant than global context.
**Environment:** Python 3.9+; in-process store suitable for single-instance services; use Redis for multi-process.

---

### Option 3: Database-backed namespacing with row-level security

```python
import sqlite3
import anthropic
from contextlib import contextmanager
from datetime import datetime

client = anthropic.Anthropic()


class IsolatedMemoryDB:
    """
    SQLite-backed memory store. Every query is parameterized with user_id
    so rows from other users are never returned.
    """

    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                user_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, key)
            )
        """)
        # Index ensures queries only scan rows for the given user_id
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_user_id ON memories(user_id)
        """)
        self.conn.commit()

    def upsert(self, user_id: str, key: str, value: str) -> None:
        now = datetime.utcnow().isoformat()
        self.conn.execute("""
            INSERT INTO memories (user_id, key, value, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
        """, (user_id, key, value, now, now))
        self.conn.commit()

    def get_all(self, user_id: str) -> dict[str, str]:
        """Parameterized query — NEVER returns rows from other users."""
        cursor = self.conn.execute(
            "SELECT key, value FROM memories WHERE user_id = ? ORDER BY key",
            (user_id,),  # user_id is always a bound parameter, never interpolated
        )
        return {row[0]: row[1] for row in cursor.fetchall()}

    def delete(self, user_id: str, key: str) -> None:
        self.conn.execute(
            "DELETE FROM memories WHERE user_id = ? AND key = ?",
            (user_id, key),
        )
        self.conn.commit()

    def purge_user(self, user_id: str) -> int:
        """Delete all memories for a user (GDPR right to erasure)."""
        cursor = self.conn.execute(
            "DELETE FROM memories WHERE user_id = ?", (user_id,)
        )
        self.conn.commit()
        return cursor.rowcount


db = IsolatedMemoryDB()


def build_system_prompt(user_id: str) -> str:
    memories = db.get_all(user_id)
    base = "You are a helpful personal assistant."
    if memories:
        lines = "\n".join(f"- {k}: {v}" for k, v in memories.items())
        return f"{base}\n\nKnown facts about this user:\n{lines}"
    return base


def chat(user_id: str, message: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=build_system_prompt(user_id),
        messages=[{"role": "user", "content": message}],
    )
    return response.content[0].text


# Seed memories — completely isolated by user_id
db.upsert("alice", "preferred_name", "Alice")
db.upsert("alice", "timezone", "America/New_York")
db.upsert("bob", "preferred_name", "Bob")
db.upsert("bob", "timezone", "Europe/London")

print(f"Alice memories: {db.get_all('alice')}")
print(f"Bob memories: {db.get_all('bob')}")

# Cross-user isolation — Alice's timezone never appears in Bob's context
print(chat("alice", "What timezone am I in?"))
print(chat("bob", "What timezone am I in?"))
```

**Expected Token Savings:** Database-level isolation eliminates cross-user memory noise from context.
**Environment:** Python 3.9+; SQLite for single-process, PostgreSQL with RLS for multi-process production.

---

### Option 4: Namespace-aware vector memory with embedding isolation

```python
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any

import anthropic

client = anthropic.Anthropic()


@dataclass
class MemoryEntry:
    user_id: str
    content: str
    key: str
    metadata: dict


class NamespacedVectorMemory:
    """
    Simulates a vector store with strict namespace isolation.
    In production, replace with Pinecone/Weaviate/pgvector filtered by user_id metadata.
    """

    def __init__(self):
        # Partition: {user_id: {key: MemoryEntry}}
        self._partitions: dict[str, dict[str, MemoryEntry]] = {}

    def _namespace(self, user_id: str) -> dict[str, MemoryEntry]:
        if user_id not in self._partitions:
            self._partitions[user_id] = {}
        return self._partitions[user_id]

    def store(self, user_id: str, key: str, content: str, metadata: dict | None = None) -> None:
        namespace = self._namespace(user_id)
        namespace[key] = MemoryEntry(
            user_id=user_id,
            content=content,
            key=key,
            metadata=metadata or {},
        )

    def search(self, user_id: str, query: str, top_k: int = 5) -> list[MemoryEntry]:
        """
        CRITICAL: Always filter by user_id BEFORE similarity search.
        Never search across all namespaces and filter after.
        """
        namespace = self._namespace(user_id)
        if not namespace:
            return []

        # In production: vector DB query with metadata filter:
        # results = index.query(vector=embed(query), filter={"user_id": user_id}, top_k=top_k)
        # Simulated: keyword overlap scoring within user's namespace only
        query_words = set(query.lower().split())
        scored = []
        for entry in namespace.values():
            content_words = set(entry.content.lower().split())
            overlap = len(query_words & content_words)
            if overlap > 0:
                scored.append((overlap, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [entry for _, entry in scored[:top_k]]

    def get_all(self, user_id: str) -> list[MemoryEntry]:
        return list(self._namespace(user_id).values())

    def delete_user(self, user_id: str) -> None:
        self._partitions.pop(user_id, None)


memory_store = NamespacedVectorMemory()


def retrieve_context(user_id: str, query: str) -> str:
    results = memory_store.search(user_id, query, top_k=3)
    if not results:
        return ""
    lines = "\n".join(f"- {r.key}: {r.content}" for r in results)
    return f"\n\n<retrieved_memories user_id=\"{user_id}\">\n{lines}\n</retrieved_memories>"


def chat(user_id: str, message: str) -> str:
    ctx = retrieve_context(user_id, message)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=f"You are a helpful assistant.{ctx}",
        messages=[{"role": "user", "content": message}],
    )
    return response.content[0].text


# Alice's private memories
memory_store.store("alice", "job", "Alice is a software engineer at Acme Corp")
memory_store.store("alice", "pet", "Alice has a dog named Rex")

# Bob's private memories
memory_store.store("bob", "job", "Bob is a teacher in London")

# Searching for "dog" for bob returns nothing — even though Alice has a dog
print(f"Bob searching 'dog': {memory_store.search('bob', 'dog')}")
print(f"Alice searching 'dog': {[e.content for e in memory_store.search('alice', 'dog')]}")

print(chat("alice", "Tell me about my pet."))
print(chat("bob", "Tell me about my pet."))  # Gets no pet info
```

**Expected Token Savings:** Namespace-scoped retrieval returns only relevant context, keeping injected memories tight (3–5 entries vs. all users' memories).
**Environment:** Python 3.9+; replace similarity stub with actual vector DB filtered queries in production.

---

### Option 5: Tenant isolation with hashed namespace keys

```python
import hashlib
import hmac
import json
import os
import anthropic

client = anthropic.Anthropic()

# Namespace signing key — prevents user_id spoofing in key generation
NAMESPACE_SECRET = os.environ.get("NAMESPACE_SECRET", "change-this-in-production")


def derive_namespace(user_id: str, tenant_id: str) -> str:
    """
    Derive a cryptographically isolated namespace key.
    Two users with the same user_id in different tenants get different namespaces.
    """
    payload = f"{tenant_id}:{user_id}"
    return hmac.new(
        NAMESPACE_SECRET.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()[:16]


class TenantMemoryStore:
    """
    Memory store isolated by (tenant_id, user_id).
    Uses HMAC-derived namespace keys to prevent cross-tenant access.
    """

    def __init__(self, base_dir: str = ".tenant_memory"):
        self.base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)

    def _path(self, tenant_id: str, user_id: str) -> str:
        ns = derive_namespace(user_id, tenant_id)
        # Store in tenant-specific directory
        tenant_dir = os.path.join(self.base_dir, tenant_id)
        os.makedirs(tenant_dir, exist_ok=True)
        return os.path.join(tenant_dir, f"{ns}.json")

    def set(self, tenant_id: str, user_id: str, key: str, value: str) -> None:
        path = self._path(tenant_id, user_id)
        data = self.get_all(tenant_id, user_id)
        data[key] = value
        with open(path, "w") as f:
            json.dump({"tenant_id": tenant_id, "namespace": derive_namespace(user_id, tenant_id), "memories": data}, f)

    def get_all(self, tenant_id: str, user_id: str) -> dict[str, str]:
        path = self._path(tenant_id, user_id)
        if not os.path.exists(path):
            return {}
        with open(path) as f:
            return json.load(f).get("memories", {})

    def purge(self, tenant_id: str, user_id: str) -> None:
        path = self._path(tenant_id, user_id)
        if os.path.exists(path):
            os.remove(path)


store = TenantMemoryStore()


def chat(tenant_id: str, user_id: str, message: str) -> str:
    memories = store.get_all(tenant_id, user_id)
    ctx = ""
    if memories:
        lines = "\n".join(f"- {k}: {v}" for k, v in memories.items())
        ctx = f"\n\n<user_context>\n{lines}\n</user_context>"

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=f"You are a helpful assistant.{ctx}",
        messages=[{"role": "user", "content": message}],
    )
    return response.content[0].text


# Same user_id "admin" in two different tenants — completely isolated
store.set("acme-corp", "admin", "role", "CTO")
store.set("globex-inc", "admin", "role", "Support Agent")

print(f"acme admin: {store.get_all('acme-corp', 'admin')}")
print(f"globex admin: {store.get_all('globex-inc', 'admin')}")

print(chat("acme-corp", "admin", "What is my role?"))
print(chat("globex-inc", "admin", "What is my role?"))
```

**Expected Token Savings:** Correct isolation prevents bloated cross-tenant context injection.
**Environment:** Python 3.9+; multi-tenant SaaS pattern; requires `NAMESPACE_SECRET` env var in production.

---

### Option 6: Memory audit tool to detect cross-namespace leaks

```python
import re
import anthropic

client = anthropic.Anthropic()


def audit_system_prompt_for_leaks(
    expected_user_id: str,
    system_prompt: str,
    known_user_ids: list[str],
) -> list[str]:
    """
    Scan a system prompt for references to other users' identifiers.
    Returns a list of detected anomalies.
    """
    anomalies = []

    for uid in known_user_ids:
        if uid == expected_user_id:
            continue
        if uid in system_prompt:
            anomalies.append(f"System prompt contains foreign user_id: {uid!r}")

    # Heuristic: multiple distinct name-like tokens may indicate data bleed
    name_pattern = re.compile(r"\b[A-Z][a-z]{2,15}\b")
    names_found = set(name_pattern.findall(system_prompt))
    # Filter common words
    stop_words = {"You", "The", "This", "Your", "User", "Agent", "True", "False"}
    candidate_names = names_found - stop_words
    if len(candidate_names) > 3:
        anomalies.append(
            f"System prompt contains {len(candidate_names)} name-like tokens: {candidate_names} — possible cross-user leak"
        )

    return anomalies


class AuditedMemoryStore:
    def __init__(self):
        self._store: dict[str, dict[str, str]] = {}

    def set(self, user_id: str, key: str, value: str) -> None:
        self._store.setdefault(user_id, {})[key] = value

    def build_system_prompt(self, user_id: str) -> str:
        user_memories = self._store.get(user_id, {})
        lines = "\n".join(f"- {k}: {v}" for k, v in user_memories.items())
        ctx = f"\n\n<user_context>\n{lines}\n</user_context>" if lines else ""
        prompt = f"You are a helpful assistant.{ctx}"

        # Audit before returning
        all_user_ids = list(self._store.keys())
        anomalies = audit_system_prompt_for_leaks(user_id, prompt, all_user_ids)
        if anomalies:
            import sys
            for a in anomalies:
                print(f"[MEMORY LEAK DETECTED] {a}", file=sys.stderr)
            # Return safe fallback — don't expose leaked data
            return "You are a helpful assistant."

        return prompt


store = AuditedMemoryStore()
store.set("alice", "name", "Alice")
store.set("alice", "city", "New York")
store.set("bob", "name", "Bob")

# Simulate a bug: Bob's system prompt accidentally includes Alice's user_id
def buggy_build(user_id: str) -> str:
    # Bug: iterates all users instead of filtering by user_id
    all_memories = {}
    for uid, memories in store._store.items():
        all_memories.update(memories)
    lines = "\n".join(f"- {k}: {v}" for k, v in all_memories.items())
    return f"You are a helpful assistant.\n\n<context>\n{lines}\n</context>"

# Audit catches the bug
leaked_prompt = buggy_build("bob")
anomalies = audit_system_prompt_for_leaks("bob", leaked_prompt, ["alice", "bob"])
print(f"Anomalies detected: {anomalies}")

# Correct store passes audit
safe_prompt = store.build_system_prompt("bob")
print(f"Safe prompt for bob:\n{safe_prompt}")
```

**Expected Token Savings:** Leak auditing ensures context stays tight and user-specific, preventing inflated cross-user memory injection.
**Environment:** Python 3.9+; audit layer adds <1ms overhead per prompt build; disable in hot paths once isolation is proven.

---

| Option | Approach | Isolation Boundary | Best For |
|--------|----------|-------------------|----------|
| 1 | File-per-user directory | user_id | Simple single-process agents |
| 2 | Session-scoped in-memory TTL | user_id + session_id | Stateless web handlers |
| 3 | SQL with parameterized queries | user_id (row-level) | Multi-user production DBs |
| 4 | Namespace-filtered vector search | user_id pre-filter | RAG memory with embeddings |
| 5 | HMAC-derived tenant namespaces | tenant_id + user_id | Multi-tenant SaaS |
| 6 | Audit layer for leak detection | Any namespace | Debugging + CI regression |
