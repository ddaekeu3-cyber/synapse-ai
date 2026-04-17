---
title: "Agent Doesn't Implement Cross-Session Data Isolation"
description: "Agents that share in-process state across sessions risk leaking one user's data into another's context: cached tool results, conversation summaries, or retrieved documents stored globally can be read by the next session to use the same cache key. Implement cross-session data isolation with session-scoped storage, cache key namespacing, and access control checks that prevent any session from reading data written by another."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-cross-session-data-isolation
tags: [session-isolation, data-leakage, cache-namespacing, multi-tenant, access-control, privacy]
symptoms:
  - "Cached tool results from one user session are returned to a different user's query"
  - "Global in-process cache shares keys across all sessions without namespacing"
  - "Conversation summaries or retrieved documents persist beyond the session that created them"
  - "Two concurrent sessions for different users can observe each other's tool results"
  - "No access control check before serving cached data to a requesting session"
---

## Why This Happens

Caching layers are built for performance: the same input should return the same cached output without re-fetching. In single-user agents this is safe. In multi-tenant agents, a cache key derived from query text alone — without user or session identity — means that user B's query that matches user A's cached response returns user A's data. The fix requires that every cached artifact is namespaced to the session or user that created it, and that reads are gated by an ownership check before returning data.

## Solution 1: Session Identity

```python
import hashlib
import time
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SessionIdentity:
    session_id: str
    user_id: str
    tenant_id: str
    created_at: float

    def namespace(self) -> str:
        """Stable namespace string for cache key prefixing."""
        return f"{self.tenant_id}::{self.user_id}::{self.session_id}"

    def cache_key(self, raw_key: str) -> str:
        """Produces a namespaced cache key that cannot collide across sessions."""
        combined = f"{self.namespace()}::{raw_key}"
        return hashlib.sha256(combined.encode()).hexdigest()[:32]

    def owns(self, other_namespace: str) -> bool:
        return other_namespace == self.namespace()
```

## Solution 2: Session-Scoped Cache

```python
import time
from threading import Lock
from typing import Any, Dict, Optional, Tuple


@dataclass
class IsolatedCacheEntry:
    value: Any
    owner_namespace: str
    created_at: float
    ttl_seconds: float

    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl_seconds


class SessionIsolatedCache:
    """
    Cache that namespaces every entry by session identity.
    Read access is denied if the requesting session does not own the entry.
    """

    def __init__(self, max_entries: int = 10000, default_ttl: float = 300.0):
        self._lock = Lock()
        self._store: Dict[str, IsolatedCacheEntry] = {}
        self._max = max_entries
        self._default_ttl = default_ttl
        self._unauthorized_reads = 0

    def set(
        self,
        identity: SessionIdentity,
        raw_key: str,
        value: Any,
        ttl_seconds: Optional[float] = None,
    ) -> None:
        cache_key = identity.cache_key(raw_key)
        entry = IsolatedCacheEntry(
            value=value,
            owner_namespace=identity.namespace(),
            created_at=time.time(),
            ttl_seconds=ttl_seconds or self._default_ttl,
        )
        with self._lock:
            self._evict()
            self._store[cache_key] = entry

    def get(
        self, identity: SessionIdentity, raw_key: str
    ) -> Tuple[bool, Any]:
        """
        Returns (found, value).
        Returns (False, None) if not found, expired, or unauthorized.
        """
        cache_key = identity.cache_key(raw_key)
        with self._lock:
            entry = self._store.get(cache_key)
            if entry is None:
                return False, None
            if entry.is_expired():
                del self._store[cache_key]
                return False, None
            if not identity.owns(entry.owner_namespace):
                self._unauthorized_reads += 1
                return False, None
            return True, entry.value

    def invalidate_session(self, identity: SessionIdentity) -> int:
        """Remove all entries owned by this session. Returns count removed."""
        ns = identity.namespace()
        with self._lock:
            to_remove = [k for k, e in self._store.items() if e.owner_namespace == ns]
            for k in to_remove:
                del self._store[k]
        return len(to_remove)

    def _evict(self) -> None:
        now = time.time()
        expired = [k for k, e in self._store.items() if e.is_expired()]
        for k in expired:
            del self._store[k]
        while len(self._store) >= self._max:
            oldest = min(self._store, key=lambda k: self._store[k].created_at)
            del self._store[oldest]

    def unauthorized_read_count(self) -> int:
        return self._unauthorized_reads
```

## Solution 3: Session-Scoped Context Store

```python
from threading import Lock
from typing import Any, Dict, Optional


class SessionContextStore:
    """
    Stores arbitrary per-session context (retrieved documents, summaries,
    tool outputs) with automatic cleanup on session end.
    No cross-session reads are possible — each session has its own dict.
    """

    def __init__(self):
        self._lock = Lock()
        self._sessions: Dict[str, Dict[str, Any]] = {}

    def set(self, identity: SessionIdentity, key: str, value: Any) -> None:
        with self._lock:
            if identity.session_id not in self._sessions:
                self._sessions[identity.session_id] = {}
            self._sessions[identity.session_id][key] = value

    def get(self, identity: SessionIdentity, key: str) -> Optional[Any]:
        with self._lock:
            return self._sessions.get(identity.session_id, {}).get(key)

    def get_all(self, identity: SessionIdentity) -> Dict[str, Any]:
        with self._lock:
            return dict(self._sessions.get(identity.session_id, {}))

    def clear_session(self, identity: SessionIdentity) -> None:
        with self._lock:
            self._sessions.pop(identity.session_id, None)

    def active_session_count(self) -> int:
        with self._lock:
            return len(self._sessions)
```

## Solution 4: Cross-Session Leak Detector

```python
import re
import time
from typing import Any, List


@dataclass
class LeakSuspicion:
    session_id: str
    detected_at: float
    leak_type: str
    description: str


class CrossSessionLeakDetector:
    """
    Scans context and cache access patterns for anomalies that suggest
    cross-session data leakage: identical content appearing in two sessions,
    unauthorized read attempts, or session IDs appearing in another session's output.
    """

    def __init__(self, cache: SessionIsolatedCache):
        self._cache = cache
        self._suspicions: List[LeakSuspicion] = []

    def check_output_for_foreign_ids(
        self,
        identity: SessionIdentity,
        output_text: str,
        known_session_ids: List[str],
    ) -> List[LeakSuspicion]:
        found = []
        for sid in known_session_ids:
            if sid == identity.session_id:
                continue
            if sid in output_text:
                suspicion = LeakSuspicion(
                    session_id=identity.session_id,
                    detected_at=time.time(),
                    leak_type="foreign_session_id_in_output",
                    description=f"Session ID '{sid}' appeared in output of session '{identity.session_id}'",
                )
                self._suspicions.append(suspicion)
                found.append(suspicion)
        return found

    def unauthorized_read_alarm(self) -> bool:
        return self._cache.unauthorized_read_count() > 0

    def suspicion_report(self) -> dict:
        return {
            "total_suspicions": len(self._suspicions),
            "unauthorized_cache_reads": self._cache.unauthorized_read_count(),
            "recent": [
                {
                    "session_id": s.session_id,
                    "type": s.leak_type,
                    "description": s.description,
                    "detected_at": s.detected_at,
                }
                for s in self._suspicions[-20:]
            ],
        }
```

## Solution 5: Session Lifecycle Manager

```python
import time
from typing import Dict, List, Optional


@dataclass
class SessionRecord:
    identity: SessionIdentity
    opened_at: float
    closed_at: Optional[float] = None
    entries_invalidated: int = 0


class SessionLifecycleManager:
    """
    Manages session open/close lifecycle and triggers cache cleanup on close.
    Ensures no session data outlives the session that created it.
    """

    def __init__(
        self,
        cache: SessionIsolatedCache,
        context_store: SessionContextStore,
    ):
        self._cache = cache
        self._context = context_store
        self._sessions: Dict[str, SessionRecord] = {}

    def open_session(self, identity: SessionIdentity) -> None:
        self._sessions[identity.session_id] = SessionRecord(
            identity=identity,
            opened_at=time.time(),
        )

    def close_session(self, identity: SessionIdentity) -> SessionRecord:
        record = self._sessions.get(identity.session_id)
        if record is None:
            record = SessionRecord(identity=identity, opened_at=time.time())

        record.closed_at = time.time()
        record.entries_invalidated = self._cache.invalidate_session(identity)
        self._context.clear_session(identity)
        return record

    def active_sessions(self) -> List[SessionRecord]:
        return [r for r in self._sessions.values() if r.closed_at is None]
```

## Solution 6: Isolation Audit Logger

```python
import time
from typing import List


class SessionIsolationAuditLogger:
    """
    Records isolation enforcement events: cache misses due to ownership checks,
    session close invalidations, and leak detector findings.
    """

    def __init__(self, max_records: int = 5000):
        self._max = max_records
        self._records: List[dict] = []

    def log_unauthorized_read_attempt(
        self, requesting_session: str, owner_session: str, key_hint: str
    ) -> None:
        self._append({
            "event": "unauthorized_cache_read",
            "requesting_session": requesting_session,
            "owner_session": owner_session,
            "key_hint": key_hint,
        })

    def log_session_cleanup(self, session_id: str, entries_removed: int) -> None:
        self._append({
            "event": "session_cleanup",
            "session_id": session_id,
            "entries_removed": entries_removed,
        })

    def log_leak_suspicion(self, suspicion: LeakSuspicion) -> None:
        self._append({
            "event": "leak_suspicion",
            "session_id": suspicion.session_id,
            "type": suspicion.leak_type,
            "description": suspicion.description,
        })

    def _append(self, record: dict) -> None:
        record["ts"] = time.time()
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append(record)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        return {
            "window_seconds": window_seconds,
            "events": len(recent),
            "unauthorized_reads": sum(1 for r in recent if r["event"] == "unauthorized_cache_read"),
            "session_cleanups": sum(1 for r in recent if r["event"] == "session_cleanup"),
            "leak_suspicions": sum(1 for r in recent if r["event"] == "leak_suspicion"),
        }
```

## Comparison

| Approach | Key Namespacing | Ownership Check | Session Cleanup | Leak Detection | Audit Trail |
|---|---|---|---|---|---|
| SessionIsolatedCache | Yes (SHA-256) | Yes (on read) | Yes (invalidate) | No | No |
| SessionContextStore | Yes (session dict) | Yes (structural) | Yes (clear) | No | No |
| CrossSessionLeakDetector | No | No | No | Yes | No |
| SessionLifecycleManager | No | No | Yes (on close) | No | No |
| SessionIsolationAuditLogger | No | No | No | No | Yes |

**Best for production**: Call `SessionLifecycleManager.close_session()` in a `finally` block for every request handler — guaranteed cleanup even on exceptions. Use `SessionIdentity.cache_key()` (SHA-256 of namespace + raw key) for all shared caches so namespaced keys are not guessable by one session for another's data. Alert immediately on any `unauthorized_cache_read` event in `SessionIsolationAuditLogger` — in a correctly implemented system this count should always be zero, so any non-zero value indicates a code path that bypasses the ownership check.
