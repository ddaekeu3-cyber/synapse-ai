---
title: "Agent Doesn't Implement Cross-Session Context Isolation"
description: "Agents that share in-process state across concurrent user sessions leak context between users: one session's tool results, retrieved documents, or conversation history can bleed into another session's context window. Implement strict session context isolation using per-session namespaced stores, copy-on-write context buffers, and cross-session contamination detection."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-cross-session-context-isolation
tags: [session-isolation, context-leakage, multi-tenant, data-isolation, session-security, context-namespacing]
symptoms:
  - "User A sees documents retrieved for User B in their context window"
  - "Shared global tool result cache returns results from another session"
  - "Conversation history from a previous session leaks into a new session"
  - "In-process singleton stores are keyed only by tool name, not session ID"
  - "Concurrent session load testing reveals intermittent data mixing"
---

## Why This Happens

When agents store tool results, retrieved documents, or conversation turns in module-level or class-level singletons, concurrent sessions share the same memory. A late-arriving response from one session's tool call can overwrite the buffer that another session is reading. Isolation requires every context store to be keyed by an opaque session ID that is generated at session creation, never reused, and passed through every layer of the agent's call stack. The session ID must be propagated as an explicit parameter — relying on thread-local storage is fragile in async runtimes.

## Solution 1: Session Context Namespace

```python
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SessionContextNamespace:
    session_id: str
    user_id: str
    created_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def create(user_id: str, ttl_seconds: float = 3600.0) -> "SessionContextNamespace":
        return SessionContextNamespace(
            session_id=secrets.token_hex(32),
            user_id=user_id,
            created_at=time.time(),
            expires_at=time.time() + ttl_seconds,
        )

    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at
```

## Solution 2: Isolated Session Context Store

```python
import threading
import time
from typing import Any, Dict, Iterator, Optional


class IsolatedSessionContextStore:
    """
    Stores arbitrary key-value context scoped strictly to a session_id.
    No data from one session is accessible by another session's key lookups.
    """

    def __init__(self, max_sessions: int = 10000):
        self._max = max_sessions
        self._store: Dict[str, Dict[str, Any]] = {}
        self._access_times: Dict[str, float] = {}
        self._lock = threading.Lock()

    def set(self, session_id: str, key: str, value: Any) -> None:
        with self._lock:
            self._evict_if_needed()
            if session_id not in self._store:
                self._store[session_id] = {}
            self._store[session_id][key] = value
            self._access_times[session_id] = time.time()

    def get(self, session_id: str, key: str, default: Any = None) -> Any:
        with self._lock:
            session_data = self._store.get(session_id)
            if session_data is None:
                return default
            self._access_times[session_id] = time.time()
            return session_data.get(key, default)

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            self._store.pop(session_id, None)
            self._access_times.pop(session_id, None)

    def session_keys(self, session_id: str) -> list:
        with self._lock:
            return list(self._store.get(session_id, {}).keys())

    def _evict_if_needed(self) -> None:
        if len(self._store) < self._max:
            return
        # evict least-recently-used session
        oldest = min(self._access_times, key=self._access_times.get)
        self._store.pop(oldest, None)
        self._access_times.pop(oldest, None)
```

## Solution 3: Session-Scoped Tool Result Buffer

```python
import copy
import time
from typing import Any, Dict, List, Optional


class SessionScopedToolResultBuffer:
    """
    Stores tool call results per session. Results from one session
    are never visible when querying with a different session_id.
    Returns deep copies to prevent shared-reference mutations.
    """

    def __init__(self, max_results_per_session: int = 200):
        self._max = max_results_per_session
        self._buffers: Dict[str, List[dict]] = {}
        self._store = IsolatedSessionContextStore()

    def append(
        self,
        session_id: str,
        tool_name: str,
        result: Any,
        metadata: Optional[dict] = None,
    ) -> None:
        record = {
            "tool_name": tool_name,
            "result": copy.deepcopy(result),
            "recorded_at": time.time(),
            "metadata": metadata or {},
        }
        existing: List[dict] = self._store.get(session_id, "_results", [])
        existing = list(existing)  # copy before mutating
        existing.append(record)
        if len(existing) > self._max:
            existing = existing[-self._max:]
        self._store.set(session_id, "_results", existing)

    def get_results(
        self,
        session_id: str,
        tool_name: Optional[str] = None,
    ) -> List[dict]:
        results = self._store.get(session_id, "_results", [])
        if tool_name:
            results = [r for r in results if r["tool_name"] == tool_name]
        return copy.deepcopy(results)

    def clear_session(self, session_id: str) -> None:
        self._store.delete_session(session_id)
```

## Solution 4: Cross-Session Contamination Detector

```python
import hashlib
import time
from typing import Dict, List, Optional, Set


class CrossSessionContaminationDetector:
    """
    Detects when content associated with one session_id appears in
    a context that is being assembled for a different session.
    Uses content fingerprints registered at result-write time.
    """

    def __init__(self, max_fingerprints_per_session: int = 500):
        self._max = max_fingerprints_per_session
        self._fingerprints: Dict[str, Set[str]] = {}   # session_id -> set of hashes
        self._hash_to_session: Dict[str, str] = {}     # hash -> originating session_id

    def register(self, session_id: str, content: str) -> str:
        fp = hashlib.sha256(content.encode()).hexdigest()[:16]
        if session_id not in self._fingerprints:
            self._fingerprints[session_id] = set()
        self._fingerprints[session_id].add(fp)
        self._hash_to_session[fp] = session_id
        # cap per-session fingerprints
        if len(self._fingerprints[session_id]) > self._max:
            oldest = next(iter(self._fingerprints[session_id]))
            self._fingerprints[session_id].discard(oldest)
        return fp

    def check(self, requesting_session_id: str, content: str) -> Optional[dict]:
        """
        Returns a contamination report if the content originated in a
        different session, or None if the content is safe.
        """
        fp = hashlib.sha256(content.encode()).hexdigest()[:16]
        origin = self._hash_to_session.get(fp)
        if origin and origin != requesting_session_id:
            return {
                "contamination_detected": True,
                "fingerprint": fp,
                "origin_session_id": origin,
                "requesting_session_id": requesting_session_id,
                "detected_at": time.time(),
            }
        return None

    def purge_session(self, session_id: str) -> None:
        fps = self._fingerprints.pop(session_id, set())
        for fp in fps:
            self._hash_to_session.pop(fp, None)
```

## Solution 5: Session Context Lifecycle Manager

```python
import time
from typing import Callable, Dict, List, Optional


class SessionContextLifecycleManager:
    """
    Creates, validates, and destroys session namespaces.
    Enforces session expiry and triggers cleanup callbacks
    when sessions are terminated.
    """

    def __init__(
        self,
        store: IsolatedSessionContextStore,
        result_buffer: SessionScopedToolResultBuffer,
        contamination_detector: CrossSessionContaminationDetector,
    ):
        self._store = store
        self._buffer = result_buffer
        self._detector = contamination_detector
        self._sessions: Dict[str, SessionContextNamespace] = {}
        self._cleanup_hooks: List[Callable[[str], None]] = []

    def create_session(self, user_id: str, ttl_seconds: float = 3600.0) -> SessionContextNamespace:
        ns = SessionContextNamespace.create(user_id, ttl_seconds)
        self._sessions[ns.session_id] = ns
        return ns

    def validate_session(self, session_id: str) -> bool:
        ns = self._sessions.get(session_id)
        if ns is None:
            return False
        if ns.is_expired():
            self.destroy_session(session_id)
            return False
        return True

    def destroy_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._store.delete_session(session_id)
        self._buffer.clear_session(session_id)
        self._detector.purge_session(session_id)
        for hook in self._cleanup_hooks:
            try:
                hook(session_id)
            except Exception:
                pass

    def add_cleanup_hook(self, fn: Callable[[str], None]) -> None:
        self._cleanup_hooks.append(fn)

    def sweep_expired(self) -> List[str]:
        expired = [sid for sid, ns in list(self._sessions.items()) if ns.is_expired()]
        for sid in expired:
            self.destroy_session(sid)
        return expired

    def active_session_count(self) -> int:
        return len(self._sessions)
```

## Solution 6: Context Isolation Audit Reporter

```python
import time
from typing import List


class ContextIsolationAuditReporter:
    """
    Aggregates contamination detection events and session lifecycle
    metrics into a security audit report.
    """

    def __init__(
        self,
        lifecycle_manager: SessionContextLifecycleManager,
        detector: CrossSessionContaminationDetector,
    ):
        self._lifecycle = lifecycle_manager
        self._detector = detector
        self._contamination_events: List[dict] = []

    def record_contamination(self, event: dict) -> None:
        self._contamination_events.append({**event, "logged_at": time.time()})

    def report(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [e for e in self._contamination_events if e["logged_at"] >= cutoff]
        involved_sessions = set()
        for e in recent:
            involved_sessions.add(e.get("origin_session_id", ""))
            involved_sessions.add(e.get("requesting_session_id", ""))

        return {
            "generated_at": time.time(),
            "window_seconds": window_seconds,
            "active_sessions": self._lifecycle.active_session_count(),
            "contamination_events": len(recent),
            "sessions_involved_in_contamination": len(involved_sessions),
            "severity": "critical" if recent else "ok",
        }
```

## Comparison

| Approach | Session Namespacing | Result Isolation | Contamination Detection | Lifecycle Management | Audit |
|---|---|---|---|---|---|
| IsolatedSessionContextStore | Yes (keyed by session_id) | Yes | No | No | No |
| SessionScopedToolResultBuffer | Via store | Yes (deep copy) | No | No | No |
| CrossSessionContaminationDetector | No | No | Yes (fingerprint) | No | No |
| SessionContextLifecycleManager | Via namespace | Via store/buffer | Via detector | Yes | No |
| ContextIsolationAuditReporter | No | No | Via detector | No | Yes |

**Best for production**: Pass `session_id` as a required parameter through every layer — tool dispatcher, result buffer, context assembler. Never use thread-local storage for session identity in async Python; coroutines migrate between threads. Run `SessionContextLifecycleManager.sweep_expired()` on a 60-second cron to reclaim memory from abandoned sessions. Enable `CrossSessionContaminationDetector` in staging and log every contamination event as a `severity=critical` security alert — any detection indicates a session isolation bug that must be fixed before the code ships to production.
