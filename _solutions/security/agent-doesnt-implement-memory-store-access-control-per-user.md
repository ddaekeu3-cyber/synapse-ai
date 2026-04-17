---
title: "Agent Doesn't Implement Memory Store Access Control Per User"
description: "Agents with shared memory stores allow any user to read or overwrite memories belonging to other users — a personal assistant that stores user preferences, past interactions, or sensitive facts provides no isolation between accounts. Implement per-user access control on the memory store that enforces ownership checks on every read, write, and delete operation."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-memory-store-access-control-per-user
tags: [memory-access-control, per-user-isolation, memory-store-security, ownership-enforcement, multi-tenant-memory, data-isolation]
symptoms:
  - "User A can query the memory store and retrieve memories belonging to user B"
  - "Agent stores a memory without recording which user it belongs to"
  - "Memory retrieval by semantic similarity returns results from all users"
  - "No concept of memory ownership — all memories are globally accessible"
  - "Deleting a memory does not verify the requesting user owns it"
---

## Why This Happens

Memory stores are often implemented as single shared vector databases or key-value stores where each memory is identified by an ID or embedding. When retrieval is performed by semantic similarity alone, results are returned from all users' memories — the query does not include a user-scoping filter. Access control must be applied at every layer: write (tag with owner), read (filter by owner), update (verify owner), and delete (verify owner). Without this, semantic search becomes a cross-user data leak vector.

## Solution 1: Owned Memory Entry

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class MemoryVisibility(str, Enum):
    PRIVATE = "private"         # only the owning user can access
    SHARED = "shared"           # accessible to all users (agent-created facts)
    SESSION = "session"         # accessible only within the creating session


@dataclass
class OwnedMemoryEntry:
    memory_id: str
    owner_user_id: str
    session_id: str
    content: str
    embedding: Optional[List[float]] = None
    visibility: MemoryVisibility = MemoryVisibility.PRIVATE
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)

    @staticmethod
    def create(
        owner_user_id: str,
        session_id: str,
        content: str,
        visibility: MemoryVisibility = MemoryVisibility.PRIVATE,
        **kwargs,
    ) -> "OwnedMemoryEntry":
        return OwnedMemoryEntry(
            memory_id=str(uuid.uuid4()),
            owner_user_id=owner_user_id,
            session_id=session_id,
            content=content,
            visibility=visibility,
            **kwargs,
        )

    def is_accessible_by(self, user_id: str, session_id: Optional[str] = None) -> bool:
        if self.visibility == MemoryVisibility.SHARED:
            return True
        if self.visibility == MemoryVisibility.PRIVATE:
            return self.owner_user_id == user_id
        if self.visibility == MemoryVisibility.SESSION:
            return self.session_id == session_id
        return False
```

## Solution 2: Access Control Enforcer

```python
from typing import Optional


class MemoryAccessDeniedError(Exception):
    def __init__(self, user_id: str, memory_id: str, operation: str):
        super().__init__(
            f"Access denied: user '{user_id}' cannot perform '{operation}' "
            f"on memory '{memory_id}'"
        )
        self.user_id = user_id
        self.memory_id = memory_id
        self.operation = operation


class MemoryAccessControlEnforcer:
    """
    Enforces ownership and visibility rules for every memory operation.
    All memory store methods must call enforce() before proceeding.
    """

    def __init__(self, audit_fn=None):
        self._audit_fn = audit_fn
        self._denied_count = 0
        self._allowed_count = 0

    def enforce(
        self,
        requesting_user_id: str,
        memory: OwnedMemoryEntry,
        operation: str,
        requesting_session_id: Optional[str] = None,
    ) -> None:
        allowed = memory.is_accessible_by(requesting_user_id, requesting_session_id)

        # Write/delete operations require strict ownership (not just visibility)
        if operation in ("write", "update", "delete"):
            allowed = memory.owner_user_id == requesting_user_id

        if allowed:
            self._allowed_count += 1
        else:
            self._denied_count += 1
            if self._audit_fn:
                self._audit_fn({
                    "user_id": requesting_user_id,
                    "memory_id": memory.memory_id,
                    "owner_user_id": memory.owner_user_id,
                    "operation": operation,
                    "visibility": memory.visibility.value,
                })
            raise MemoryAccessDeniedError(requesting_user_id, memory.memory_id, operation)

    def stats(self) -> dict:
        total = self._allowed_count + self._denied_count
        return {
            "allowed": self._allowed_count,
            "denied": self._denied_count,
            "denial_rate": round(self._denied_count / total, 4) if total > 0 else 0.0,
        }
```

## Solution 3: User-Scoped Memory Store

```python
import math
from threading import Lock
from typing import Any, Callable, Dict, List, Optional


class UserScopedMemoryStore:
    """
    In-memory store with per-user namespace enforcement.
    Semantic search is scoped to the requesting user's accessible memories.
    """

    def __init__(self, enforcer: MemoryAccessControlEnforcer):
        self._enforcer = enforcer
        self._memories: Dict[str, OwnedMemoryEntry] = {}
        self._lock = Lock()

    def write(self, memory: OwnedMemoryEntry, requesting_user_id: str) -> str:
        # Verify the requesting user is the owner (new memories only)
        if memory.owner_user_id != requesting_user_id:
            raise MemoryAccessDeniedError(requesting_user_id, memory.memory_id, "write")
        with self._lock:
            self._memories[memory.memory_id] = memory
        return memory.memory_id

    def read(
        self,
        memory_id: str,
        requesting_user_id: str,
        requesting_session_id: Optional[str] = None,
    ) -> OwnedMemoryEntry:
        with self._lock:
            memory = self._memories.get(memory_id)
        if memory is None:
            raise KeyError(f"Memory '{memory_id}' not found")
        self._enforcer.enforce(requesting_user_id, memory, "read", requesting_session_id)
        return memory

    def delete(self, memory_id: str, requesting_user_id: str) -> None:
        with self._lock:
            memory = self._memories.get(memory_id)
        if memory is None:
            return
        self._enforcer.enforce(requesting_user_id, memory, "delete")
        with self._lock:
            self._memories.pop(memory_id, None)

    def search(
        self,
        query_embedding: List[float],
        requesting_user_id: str,
        requesting_session_id: Optional[str] = None,
        top_k: int = 10,
    ) -> List[OwnedMemoryEntry]:
        with self._lock:
            candidates = list(self._memories.values())

        # Filter to accessible memories first
        accessible = [
            m for m in candidates
            if m.is_accessible_by(requesting_user_id, requesting_session_id)
            and m.embedding is not None
        ]

        if not accessible or not query_embedding:
            return accessible[:top_k]

        # Rank by cosine similarity
        def cosine(a, b):
            dot = sum(x * y for x, y in zip(a, b))
            na = math.sqrt(sum(x * x for x in a))
            nb = math.sqrt(sum(x * x for x in b))
            return dot / (na * nb) if na and nb else 0.0

        ranked = sorted(
            accessible,
            key=lambda m: cosine(query_embedding, m.embedding),
            reverse=True,
        )
        return ranked[:top_k]

    def list_user_memories(self, user_id: str) -> List[OwnedMemoryEntry]:
        with self._lock:
            return [m for m in self._memories.values() if m.owner_user_id == user_id]
```

## Solution 4: Memory Ownership Transfer Handler

```python
import time
from typing import Optional


class MemoryOwnershipTransferHandler:
    """
    Handles admin-initiated ownership transfers when a user account is
    merged, deleted, or handed off — with full audit logging.
    """

    def __init__(self, store: UserScopedMemoryStore, audit_fn=None):
        self._store = store
        self._audit_fn = audit_fn

    def transfer(
        self,
        memory_id: str,
        from_user_id: str,
        to_user_id: str,
        admin_user_id: str,
    ) -> None:
        with self._store._lock:
            memory = self._store._memories.get(memory_id)
        if memory is None:
            raise KeyError(f"Memory '{memory_id}' not found")
        if memory.owner_user_id != from_user_id:
            raise ValueError(
                f"Memory '{memory_id}' is not owned by user '{from_user_id}'"
            )
        memory.owner_user_id = to_user_id
        memory.updated_at = time.time()
        if self._audit_fn:
            self._audit_fn({
                "event": "ownership_transfer",
                "memory_id": memory_id,
                "from_user": from_user_id,
                "to_user": to_user_id,
                "admin": admin_user_id,
                "ts": time.time(),
            })

    def purge_user_memories(self, user_id: str, admin_user_id: str) -> int:
        memories = self._store.list_user_memories(user_id)
        for memory in memories:
            self._store.delete(memory.memory_id, user_id)
        if self._audit_fn and memories:
            self._audit_fn({
                "event": "user_memory_purge",
                "user_id": user_id,
                "count": len(memories),
                "admin": admin_user_id,
                "ts": time.time(),
            })
        return len(memories)
```

## Solution 5: Memory Access Audit Logger

```python
import json
import time
from pathlib import Path
from threading import Lock
from typing import List


class MemoryAccessAuditLogger:
    """
    Persists memory access denial events for security review.
    High denial rates from a single user indicate probing attempts.
    """

    def __init__(self, path: str = "/tmp/memory_access_audit.jsonl"):
        self._path = Path(path)
        self._lock = Lock()
        self._events: List[dict] = []

    def record(self, event: dict) -> None:
        record = {"ts": time.time(), **event}
        self._events.append(record)
        with self._lock:
            with self._path.open("a") as f:
                f.write(json.dumps(record) + "\n")

    def top_denied_users(self, window_seconds: float = 3600.0, top_n: int = 10) -> List[dict]:
        cutoff = time.time() - window_seconds
        counts: dict = {}
        for event in self._events:
            if event.get("ts", 0) >= cutoff:
                uid = event.get("user_id", "unknown")
                counts[uid] = counts.get(uid, 0) + 1
        return sorted(
            [{"user_id": u, "denials": c} for u, c in counts.items()],
            key=lambda x: -x["denials"],
        )[:top_n]

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [e for e in self._events if e.get("ts", 0) >= cutoff]
        ops: dict = {}
        for e in recent:
            op = e.get("operation", "unknown")
            ops[op] = ops.get(op, 0) + 1
        return {
            "window_seconds": window_seconds,
            "total_denials": len(recent),
            "by_operation": ops,
        }
```

## Solution 6: Memory Access Control Dashboard

```python
import time


class MemoryAccessControlDashboard:
    """
    Operational view of access control enforcement: denial rates,
    top probing users, and memory distribution across users.
    """

    def __init__(
        self,
        store: UserScopedMemoryStore,
        enforcer: MemoryAccessControlEnforcer,
        audit_logger: MemoryAccessAuditLogger,
    ):
        self._store = store
        self._enforcer = enforcer
        self._audit = audit_logger

    def render(self) -> dict:
        with self._store._lock:
            all_memories = list(self._store._memories.values())

        user_counts: dict = {}
        for m in all_memories:
            user_counts[m.owner_user_id] = user_counts.get(m.owner_user_id, 0) + 1

        vis_counts: dict = {}
        for m in all_memories:
            v = m.visibility.value
            vis_counts[v] = vis_counts.get(v, 0) + 1

        return {
            "generated_at": time.time(),
            "total_memories": len(all_memories),
            "unique_owners": len(user_counts),
            "visibility_distribution": vis_counts,
            "enforcer_stats": self._enforcer.stats(),
            "top_denied_users_1h": self._audit.top_denied_users(3600.0),
            "audit_summary_1h": self._audit.summary(3600.0),
        }
```

## Comparison

| Approach | Ownership Tagging | Visibility Scoping | Enforced Read/Write/Delete | Semantic Search Scoping | Audit |
|---|---|---|---|---|---|
| OwnedMemoryEntry | Yes | Yes (3 levels) | No | No | No |
| MemoryAccessControlEnforcer | No | Via entry | Yes | No | Via callback |
| UserScopedMemoryStore | Via entry | Via enforcer | Via enforcer | Yes (pre-filter) | Via enforcer |
| MemoryOwnershipTransferHandler | Via store | No | Via store | No | Via callback |
| MemoryAccessAuditLogger | No | No | No | No | Yes (JSONL) |
| MemoryAccessControlDashboard | No | No | No | No | Yes |

**Best for production**: Apply user-scoping as a pre-filter in semantic search before computing cosine similarity — never retrieve all memories and then filter by ownership post-hoc, as this exposes other users' embedding vectors to the query. Store `owner_user_id` as an indexed column in your vector database metadata so the ownership filter is applied at the database layer, not in application code. Use `MemoryVisibility.SHARED` only for agent-created factual memories (e.g., knowledge base entries) that are genuinely public; user-created memories should default to PRIVATE. Alert on `top_denied_users` with more than 10 denials per hour — this pattern indicates a user systematically probing for other users' memory IDs.
