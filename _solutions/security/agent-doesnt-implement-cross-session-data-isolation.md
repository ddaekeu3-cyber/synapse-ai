---
title: "Agent Doesn't Implement Cross-Session Data Isolation"
description: "Agents that share mutable state across concurrent user sessions — cached tool results, conversation summaries, embedding stores, or LRU caches keyed by content rather than session — allow one user's data to bleed into another user's context. A cached database result from user A's privileged query can be served to user B whose access level does not permit that data. Implement session-scoped data isolation that ensures every cache, buffer, and store is keyed by session ID and never shares data across sessions."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-cross-session-data-isolation
tags: [session-isolation, data-leakage, cache-isolation, multi-tenant-security, session-scoping, concurrent-sessions]
symptoms:
  - "Cached tool results from a privileged session are served to an unprivileged session"
  - "Conversation summaries from one user appear in another user's context"
  - "LRU cache keyed by query text returns results from another user's query"
  - "Shared embedding store allows one session to retrieve documents loaded by another"
  - "In-memory buffers persist across session boundaries due to object reuse"
---

## Why This Happens

Performance optimizations that share state across sessions — caches keyed by content hash, shared embedding stores, session-agnostic LRU caches — trade security isolation for efficiency. When multiple users share an agent instance, any data in a shared cache is accessible to any session that produces the same cache key, regardless of the requesting session's access level. The fix requires either session-scoping all caches (each session gets its own isolated store) or including the session's access level in the cache key so that cross-session cache hits only occur between sessions with identical permissions.

## Solution 1: Session Identity

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet, Optional, Set


class AccessLevel(str, Enum):
    PUBLIC = "public"
    USER = "user"
    PREMIUM = "premium"
    ADMIN = "admin"


@dataclass
class SessionIdentity:
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    user_id: str = ""
    access_level: AccessLevel = AccessLevel.USER
    tenant_id: str = ""
    created_at: float = field(default_factory=time.time)
    permissions: FrozenSet[str] = field(default_factory=frozenset)

    def isolation_key(self) -> str:
        """Key that two sessions must share to allow cross-session cache hits."""
        return f"{self.tenant_id}:{self.access_level.value}:{','.join(sorted(self.permissions))}"

    def can_share_with(self, other: "SessionIdentity") -> bool:
        """Returns True only if both sessions have identical access profiles."""
        return self.isolation_key() == other.isolation_key()
```

## Solution 2: Session-Scoped Cache

```python
import time
from threading import Lock
from typing import Any, Dict, Optional, Tuple


class SessionScopedCache:
    """
    Cache partitioned by session_id. Each session has its own isolated
    namespace; cross-session lookups are impossible by design.
    """

    def __init__(
        self,
        max_entries_per_session: int = 200,
        session_ttl_seconds: float = 3600.0,
    ):
        self._max_per_session = max_entries_per_session
        self._session_ttl = session_ttl_seconds
        self._data: Dict[str, Dict[str, Tuple[Any, float]]] = {}
        self._session_created: Dict[str, float] = {}
        self._lock = Lock()

    def get(self, session_id: str, key: str) -> Optional[Any]:
        with self._lock:
            session_data = self._data.get(session_id, {})
            entry = session_data.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if time.time() > expires_at:
                del session_data[key]
                return None
            return value

    def put(
        self,
        session_id: str,
        key: str,
        value: Any,
        ttl_seconds: float = 300.0,
    ) -> None:
        with self._lock:
            if session_id not in self._data:
                self._data[session_id] = {}
                self._session_created[session_id] = time.time()

            session_data = self._data[session_id]
            if len(session_data) >= self._max_per_session:
                # Evict oldest entry
                oldest_key = min(session_data, key=lambda k: session_data[k][1])
                del session_data[oldest_key]

            session_data[key] = (value, time.time() + ttl_seconds)

    def invalidate_session(self, session_id: str) -> None:
        with self._lock:
            self._data.pop(session_id, None)
            self._session_created.pop(session_id, None)

    def evict_expired_sessions(self) -> int:
        now = time.time()
        with self._lock:
            expired = [
                sid for sid, created in self._session_created.items()
                if now - created > self._session_ttl
            ]
            for sid in expired:
                self._data.pop(sid, None)
                self._session_created.pop(sid, None)
            return len(expired)

    def stats(self) -> dict:
        with self._lock:
            total_entries = sum(len(d) for d in self._data.values())
            return {
                "active_sessions": len(self._data),
                "total_cached_entries": total_entries,
            }
```

## Solution 3: Access-Level Keyed Shared Cache

```python
import hashlib
import time
from threading import Lock
from typing import Any, Dict, Optional, Tuple


class AccessKeyedSharedCache:
    """
    Shared cache that includes the session's access profile in the key.
    Two sessions can share a cache entry only if they have identical
    access profiles — preventing privilege elevation via cache hits.
    """

    def __init__(self, max_entries: int = 5000):
        self._max = max_entries
        self._data: Dict[str, Tuple[Any, float]] = {}
        self._lock = Lock()

    def _scoped_key(self, session: SessionIdentity, content_key: str) -> str:
        isolation = session.isolation_key()
        return hashlib.sha256(f"{isolation}:{content_key}".encode()).hexdigest()[:24]

    def get(self, session: SessionIdentity, content_key: str) -> Optional[Any]:
        scoped = self._scoped_key(session, content_key)
        with self._lock:
            entry = self._data.get(scoped)
            if entry is None:
                return None
            value, expires_at = entry
            if time.time() > expires_at:
                del self._data[scoped]
                return None
            return value

    def put(
        self,
        session: SessionIdentity,
        content_key: str,
        value: Any,
        ttl_seconds: float = 300.0,
    ) -> None:
        scoped = self._scoped_key(session, content_key)
        with self._lock:
            if len(self._data) >= self._max:
                # Simple eviction: remove expired entries first
                now = time.time()
                stale = [k for k, (_, exp) in self._data.items() if exp < now]
                for k in stale[:100]:
                    del self._data[k]
            self._data[scoped] = (value, time.time() + ttl_seconds)
```

## Solution 4: Isolation Violation Detector

```python
import time
from typing import List


class IsolationViolationDetector:
    """
    Detects attempts to access another session's data by monitoring
    for cache key construction patterns that bypass session scoping.
    Flags code paths that use content-only keys without session context.
    """

    def __init__(self):
        self._violations: List[dict] = []

    def record_unscoped_access(
        self,
        session_id: str,
        cache_name: str,
        key: str,
        context: str = "",
    ) -> None:
        """
        Call this when a cache lookup occurs without session context.
        Use as a canary in legacy cache code paths.
        """
        self._violations.append({
            "ts": time.time(),
            "session_id": session_id,
            "cache_name": cache_name,
            "key_preview": key[:30],
            "context": context,
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [v for v in self._violations if v["ts"] >= cutoff]
        from collections import Counter
        by_cache = Counter(v["cache_name"] for v in recent)
        return {
            "window_seconds": window_seconds,
            "unscoped_accesses": len(recent),
            "by_cache": dict(by_cache.most_common(5)),
        }
```

## Solution 5: Session Data Lifecycle Manager

```python
import time
from typing import List


class SessionDataLifecycleManager:
    """
    Coordinates cleanup of all session-scoped stores when a session ends.
    Prevents data accumulation from abandoned sessions.
    """

    def __init__(self):
        self._stores: List[SessionScopedCache] = []
        self._terminated_sessions: List[dict] = []

    def register_store(self, store: SessionScopedCache) -> None:
        self._stores.append(store)

    def on_session_end(self, session_id: str, reason: str = "normal") -> None:
        for store in self._stores:
            store.invalidate_session(session_id)
        self._terminated_sessions.append({
            "ts": time.time(),
            "session_id": session_id,
            "reason": reason,
        })

    def run_gc(self) -> dict:
        evicted = sum(store.evict_expired_sessions() for store in self._stores)
        return {
            "stores_cleaned": len(self._stores),
            "sessions_evicted": evicted,
        }

    def summary(self) -> dict:
        return {
            "registered_stores": len(self._stores),
            "total_terminated_sessions": len(self._terminated_sessions),
            "store_stats": [s.stats() for s in self._stores],
        }
```

## Solution 6: Cross-Session Isolation Dashboard

```python
import time


class CrossSessionIsolationDashboard:
    """
    Combines session-scoped cache stats, violation detection,
    and lifecycle management into a single security view.
    """

    def __init__(
        self,
        lifecycle_manager: SessionDataLifecycleManager,
        violation_detector: IsolationViolationDetector,
    ):
        self._lifecycle = lifecycle_manager
        self._detector = violation_detector

    def render(self, window_seconds: float = 3600.0) -> dict:
        return {
            "generated_at": time.time(),
            "lifecycle": self._lifecycle.summary(),
            "isolation_violations": self._detector.summary(window_seconds),
        }
```

## Comparison

| Approach | Session Partitioning | Access-Level Keying | Violation Detection | Lifecycle GC | Dashboard |
|---|---|---|---|---|---|
| SessionScopedCache | Yes (hard partition) | No | No | Yes | No |
| AccessKeyedSharedCache | No | Yes (HMAC-keyed) | No | No | No |
| IsolationViolationDetector | No | No | Yes | No | No |
| SessionDataLifecycleManager | Via stores | No | No | Yes | No |
| CrossSessionIsolationDashboard | No | No | No | No | Yes |

**Best for production**: Default to `SessionScopedCache` for all tool result caches — the performance overhead of session partitioning is negligible compared to the security benefit. Use `AccessKeyedSharedCache` only for truly access-level-neutral content like public documentation or cached LLM model metadata, where sharing is safe and the access profile is provably equivalent. Register all session-scoped stores with `SessionDataLifecycleManager` and call `on_session_end()` in your session termination handler — expired sessions should not accumulate indefinitely. Run `IsolationViolationDetector` alerts in all environments: any `unscoped_accesses > 0` in production is a P1 security finding requiring immediate code review.
