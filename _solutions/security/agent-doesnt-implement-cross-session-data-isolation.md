---
title: "Agent Doesn't Implement Cross-Session Data Isolation"
description: "Agents that share in-memory state, caches, or tool results across user sessions allow one session's data to bleed into another — a user may receive retrieved documents from a previous user's query, see cached tool results containing other users' PII, or have their conversation history accessible to subsequent sessions reusing the same agent instance. Implement strict cross-session data isolation with session-scoped namespaces, cache key segregation, and session cleanup on termination."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-cross-session-data-isolation
tags: [session-isolation, data-leakage, cross-session-contamination, cache-segregation, pii-isolation, multi-tenant-security]
symptoms:
  - "Cached tool results from user A returned to user B with a matching query"
  - "Shared in-memory retrieval cache not keyed by user — first user's documents returned to all"
  - "Session-level context (user preferences, history) persists after session ends and affects next user"
  - "No session namespace in cache keys — cache hit possible across users"
  - "Memory or context grows indefinitely without cleanup on session termination"
---

## Why This Happens

Agent frameworks often optimize for performance by caching results globally — retrieval caches, tool output caches, and embedding caches keyed only by query content. When two users issue the same query, the second user gets the cached result from the first user's session. This is correct behavior for public data but catastrophic for personalized or sensitive data. Isolation requires that every cache and shared store be keyed by a session identifier that is cryptographically unguessable and scoped to a single authenticated user. Session cleanup must be triggered on logout, timeout, and error to prevent accumulation of abandoned session state.

## Solution 1: Session Identity

```python
import hashlib
import os
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SessionIdentity:
    session_id: str              # cryptographically random, server-generated
    user_id: str                 # authenticated user identifier
    tenant_id: str = ""          # for multi-tenant deployments
    created_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    ip_address: str = ""

    def namespace(self) -> str:
        """Stable, opaque namespace for all cache keys in this session."""
        raw = f"{self.session_id}:{self.user_id}:{self.tenant_id}"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at

    @staticmethod
    def generate(
        user_id: str,
        tenant_id: str = "",
        ttl_seconds: float = 3600.0,
        ip_address: str = "",
    ) -> "SessionIdentity":
        session_id = os.urandom(32).hex()
        return SessionIdentity(
            session_id=session_id,
            user_id=user_id,
            tenant_id=tenant_id,
            expires_at=time.time() + ttl_seconds,
            ip_address=ip_address,
        )
```

## Solution 2: Session-Scoped Cache

```python
from collections import OrderedDict
from threading import Lock
from typing import Any, Dict, Optional
import time


class SessionScopedCache:
    """
    Cache where every key is prefixed by the session namespace.
    Guarantees that two sessions with identical query keys cannot share entries.
    Supports bulk eviction of all keys belonging to a terminated session.
    """

    def __init__(self, max_total_entries: int = 100000):
        self._store: OrderedDict = OrderedDict()
        self._session_keys: Dict[str, set] = {}   # namespace -> set of full keys
        self._lock = Lock()
        self._max = max_total_entries

    def _scoped_key(self, namespace: str, key: str) -> str:
        return f"{namespace}::{key}"

    def get(self, identity: SessionIdentity, key: str) -> Optional[Any]:
        if identity.is_expired():
            return None
        ns = identity.namespace()
        full_key = self._scoped_key(ns, key)
        with self._lock:
            value = self._store.get(full_key)
            if value is not None:
                self._store.move_to_end(full_key)
            return value

    def put(self, identity: SessionIdentity, key: str, value: Any) -> None:
        if identity.is_expired():
            return
        ns = identity.namespace()
        full_key = self._scoped_key(ns, key)
        with self._lock:
            if len(self._store) >= self._max:
                self._store.popitem(last=False)
            self._store[full_key] = value
            if ns not in self._session_keys:
                self._session_keys[ns] = set()
            self._session_keys[ns].add(full_key)

    def evict_session(self, identity: SessionIdentity) -> int:
        """Remove all cache entries for this session. Returns count evicted."""
        ns = identity.namespace()
        with self._lock:
            keys = self._session_keys.pop(ns, set())
            for k in keys:
                self._store.pop(k, None)
        return len(keys)

    def stats(self) -> dict:
        with self._lock:
            return {
                "total_entries": len(self._store),
                "active_sessions": len(self._session_keys),
            }
```

## Solution 3: Session State Store

```python
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Dict, List, Optional


@dataclass
class SessionState:
    identity: SessionIdentity
    conversation_history: List[dict] = field(default_factory=list)
    user_preferences: Dict[str, Any] = field(default_factory=dict)
    tool_results: Dict[str, Any] = field(default_factory=dict)
    custom_data: Dict[str, Any] = field(default_factory=dict)
    last_active_at: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_active_at = time.time()

    def is_idle(self, idle_threshold_s: float = 1800.0) -> bool:
        return time.time() - self.last_active_at > idle_threshold_s


class SessionStateStore:
    """
    Manages per-session state with strict isolation.
    Expires idle sessions automatically.
    """

    def __init__(self, idle_timeout_s: float = 1800.0):
        self._sessions: Dict[str, SessionState] = {}
        self._lock = Lock()
        self._idle_timeout = idle_timeout_s
        self._evicted_count = 0

    def create(self, identity: SessionIdentity) -> SessionState:
        state = SessionState(identity=identity)
        with self._lock:
            self._sessions[identity.session_id] = state
        return state

    def get(self, session_id: str) -> Optional[SessionState]:
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                return None
            if state.identity.is_expired() or state.is_idle(self._idle_timeout):
                del self._sessions[session_id]
                self._evicted_count += 1
                return None
            state.touch()
            return state

    def terminate(self, session_id: str) -> bool:
        with self._lock:
            if session_id in self._sessions:
                del self._sessions[session_id]
                self._evicted_count += 1
                return True
            return False

    def evict_expired(self) -> int:
        now = time.time()
        with self._lock:
            expired = [
                sid for sid, state in self._sessions.items()
                if state.identity.is_expired() or state.is_idle(self._idle_timeout)
            ]
            for sid in expired:
                del self._sessions[sid]
            self._evicted_count += len(expired)
        return len(expired)

    def stats(self) -> dict:
        with self._lock:
            return {
                "active_sessions": len(self._sessions),
                "total_evicted": self._evicted_count,
            }
```

## Solution 4: Cross-Session Contamination Detector

```python
import time
from typing import Dict, List, Optional


class CrossSessionContaminationDetector:
    """
    Validates that data returned from shared resources is not contaminated
    with another session's identifying information.
    Checks for user IDs, session IDs, and PII patterns in returned content.
    """

    import re
    EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")
    UUID_PATTERN = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")

    def __init__(self, active_user_ids: set = None):
        self._known_ids = active_user_ids or set()
        self._violations: List[dict] = []

    def register_user(self, user_id: str) -> None:
        self._known_ids.add(user_id)

    def check(
        self,
        content: str,
        current_session: SessionIdentity,
    ) -> List[str]:
        """Returns list of contamination warnings."""
        warnings = []
        # Check if another user's ID appears in the content
        for uid in self._known_ids:
            if uid != current_session.user_id and uid in content:
                warnings.append(f"cross_session_user_id: foreign user_id '{uid[:8]}...' found in content")

        # Check for suspicious patterns
        emails = self.EMAIL_PATTERN.findall(content)
        uuids = self.UUID_PATTERN.findall(content)
        if emails:
            warnings.append(f"email_in_content: {len(emails)} email(s) found")
        if len(uuids) > 3:
            warnings.append(f"many_uuids: {len(uuids)} UUIDs in content — possible cross-session bleed")

        if warnings:
            self._violations.append({
                "ts": time.time(),
                "session_id": current_session.session_id,
                "warnings": warnings,
            })
        return warnings
```

## Solution 5: Session Lifecycle Manager

```python
import asyncio
import time
from typing import Callable, Optional


class SessionLifecycleManager:
    """
    Manages session creation, validation, and cleanup.
    Triggers cleanup callbacks (cache eviction, state deletion) on termination.
    Runs a background reaper for idle and expired sessions.
    """

    def __init__(
        self,
        state_store: SessionStateStore,
        cache: SessionScopedCache,
        idle_timeout_s: float = 1800.0,
    ):
        self._store = state_store
        self._cache = cache
        self._idle_timeout = idle_timeout_s
        self._terminated_count = 0

    def start_session(self, user_id: str, tenant_id: str = "", ttl_s: float = 3600.0) -> SessionIdentity:
        identity = SessionIdentity.generate(user_id, tenant_id, ttl_s)
        self._store.create(identity)
        return identity

    def validate(self, session_id: str) -> Optional[SessionState]:
        return self._store.get(session_id)

    def terminate_session(self, identity: SessionIdentity) -> dict:
        cache_evicted = self._cache.evict_session(identity)
        state_removed = self._store.terminate(identity.session_id)
        self._terminated_count += 1
        return {
            "session_id": identity.session_id,
            "cache_entries_evicted": cache_evicted,
            "state_removed": state_removed,
        }

    async def run_reaper(self, poll_interval_s: float = 60.0) -> None:
        while True:
            await asyncio.sleep(poll_interval_s)
            self._store.evict_expired()
```

## Solution 6: Session Isolation Dashboard

```python
import time


class SessionIsolationDashboard:
    """
    Combines session store stats, cache isolation stats, and contamination
    detection history into a single security-focused view.
    """

    def __init__(
        self,
        lifecycle: SessionLifecycleManager,
        detector: CrossSessionContaminationDetector,
    ):
        self._lifecycle = lifecycle
        self._detector = detector

    def render(self) -> dict:
        store_stats = self._lifecycle._store.stats()
        cache_stats = self._lifecycle._cache.stats()
        recent_violations = [
            v for v in self._detector._violations
            if time.time() - v["ts"] < 3600
        ]
        return {
            "generated_at": time.time(),
            "session_store": store_stats,
            "session_cache": cache_stats,
            "terminated_sessions": self._lifecycle._terminated_count,
            "contamination_violations_last_hour": len(recent_violations),
            "recent_violations": recent_violations[-5:],
        }
```

## Comparison

| Approach | Namespace Isolation | Cache Eviction | State Cleanup | Contamination Detection | Reaper |
|---|---|---|---|---|---|
| SessionScopedCache | Yes (SHA-256 prefix) | Yes (bulk by session) | No | No | No |
| SessionStateStore | Yes (session_id key) | No | Yes (expire + idle) | No | No |
| CrossSessionContaminationDetector | No | No | No | Yes | No |
| SessionLifecycleManager | Via store + cache | Via cache | Via store | No | Yes |
| SessionIsolationDashboard | No | No | No | Via detector | No |

**Best for production**: Never use query text alone as a cache key — always prefix with the session namespace. Run `SessionLifecycleManager.run_reaper()` every 60 seconds to evict idle sessions; abandoned sessions accumulate indefinitely otherwise and may hold PII in memory. Set session TTL to the minimum needed for the use case (1 hour for interactive agents, 15 minutes for API-only agents). Run `CrossSessionContaminationDetector` on all retrieved content during security audits — a violation in staging means the retrieval pipeline is not properly scoped and will leak in production.
