---
title: "Agent Doesn't Implement Cross-Session Data Isolation"
description: "Agents that share mutable state between concurrent sessions risk data leakage: one user's retrieved documents, cached tool results, or in-progress context can bleed into another user's session through shared caches, global variables, or improperly scoped memory stores. Implement strict cross-session data isolation with per-session namespacing, scope enforcement, and leak detection."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-cross-session-data-isolation
tags: [data-isolation, session-isolation, multi-tenancy, data-leakage, namespace-enforcement, cache-isolation]
symptoms:
  - "Cached tool results from one user appear in another user's conversation"
  - "Shared in-memory tool registry accumulates state across sessions"
  - "Global variables modified during one session affect concurrent sessions"
  - "No namespace separation between sessions in shared caches or stores"
  - "Audit logs cannot attribute data access to specific sessions"
---

## Why This Happens

Agents running in async servers handle multiple sessions concurrently in the same process. Shared mutable state — module-level caches, singleton tool instances, class variables, asyncio-shared queues — is invisible to session boundaries. A tool that caches its last result in a class attribute will return a previous user's data to the next caller. Isolation requires that every piece of session-specific state is keyed by session ID, that reads and writes validate the requesting session, and that session termination purges all associated state.

## Solution 1: Session Namespace Registry

```python
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set


@dataclass
class SessionNamespace:
    session_id: str
    user_id: str
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    _store: Dict[str, Any] = field(default_factory=dict)

    def get(self, key: str) -> Optional[Any]:
        self.last_accessed = time.time()
        return self._store.get(key)

    def set(self, key: str, value: Any) -> None:
        self.last_accessed = time.time()
        self._store[key] = value

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()

    def keys(self) -> Set[str]:
        return set(self._store.keys())


class SessionNamespaceRegistry:
    """
    Central registry of active session namespaces.
    All session-scoped state must be stored here, never in global scope.
    """

    def __init__(self, session_ttl_seconds: float = 3600.0):
        self._namespaces: Dict[str, SessionNamespace] = {}
        self._ttl = session_ttl_seconds
        self._lock = threading.Lock()

    def create(self, session_id: str, user_id: str) -> SessionNamespace:
        with self._lock:
            ns = SessionNamespace(session_id=session_id, user_id=user_id)
            self._namespaces[session_id] = ns
            return ns

    def get(self, session_id: str) -> Optional[SessionNamespace]:
        with self._lock:
            return self._namespaces.get(session_id)

    def destroy(self, session_id: str) -> None:
        with self._lock:
            ns = self._namespaces.pop(session_id, None)
            if ns:
                ns.clear()

    def evict_expired(self) -> int:
        cutoff = time.time() - self._ttl
        with self._lock:
            expired = [
                sid for sid, ns in self._namespaces.items()
                if ns.last_accessed < cutoff
            ]
            for sid in expired:
                self._namespaces[sid].clear()
                del self._namespaces[sid]
        return len(expired)

    def active_sessions(self) -> int:
        with self._lock:
            return len(self._namespaces)
```

## Solution 2: Isolated Tool Result Cache

```python
import time
from threading import Lock
from typing import Any, Dict, Optional, Tuple


class IsolatedToolResultCache:
    """
    Per-session tool result cache. Results are namespaced by (session_id, tool_name, args_hash)
    and can only be read back by the same session that wrote them.
    """

    def __init__(self, ttl_seconds: float = 300.0, max_entries_per_session: int = 100):
        self._cache: Dict[str, Dict[str, Tuple[Any, float]]] = {}
        self._ttl = ttl_seconds
        self._max_per_session = max_entries_per_session
        self._lock = Lock()

    def _args_hash(self, args: dict) -> str:
        import hashlib, json
        return hashlib.md5(json.dumps(args, sort_keys=True).encode()).hexdigest()[:12]

    def get(self, session_id: str, tool_name: str, args: dict) -> Optional[Any]:
        key = f"{tool_name}:{self._args_hash(args)}"
        now = time.time()
        with self._lock:
            session_cache = self._cache.get(session_id, {})
            entry = session_cache.get(key)
            if entry is None:
                return None
            value, stored_at = entry
            if now - stored_at > self._ttl:
                del session_cache[key]
                return None
            return value

    def set(self, session_id: str, tool_name: str, args: dict, value: Any) -> None:
        key = f"{tool_name}:{self._args_hash(args)}"
        with self._lock:
            if session_id not in self._cache:
                self._cache[session_id] = {}
            session_cache = self._cache[session_id]
            if len(session_cache) >= self._max_per_session:
                oldest_key = min(session_cache, key=lambda k: session_cache[k][1])
                del session_cache[oldest_key]
            session_cache[key] = (value, time.time())

    def purge_session(self, session_id: str) -> int:
        with self._lock:
            session_cache = self._cache.pop(session_id, {})
            return len(session_cache)
```

## Solution 3: Session Scope Enforcer

```python
from typing import Any, Callable


class SessionScopeViolationError(Exception):
    def __init__(self, requesting_session: str, owning_session: str, resource: str):
        super().__init__(
            f"Session scope violation: session '{requesting_session}' attempted to access "
            f"resource '{resource}' owned by session '{owning_session}'"
        )
        self.requesting_session = requesting_session
        self.owning_session = owning_session
        self.resource = resource


class SessionScopeEnforcer:
    """
    Validates that any data access is performed by the session that owns it.
    Wrap all cross-session-capable storage reads with this enforcer.
    """

    def __init__(self, violation_fn: Callable[[dict], None] = None):
        self._violation_fn = violation_fn
        self._violation_count = 0

    def check(
        self,
        requesting_session_id: str,
        resource_owner_session_id: str,
        resource_name: str,
    ) -> None:
        if requesting_session_id != resource_owner_session_id:
            self._violation_count += 1
            event = {
                "requesting": requesting_session_id,
                "owner": resource_owner_session_id,
                "resource": resource_name,
            }
            if self._violation_fn:
                self._violation_fn(event)
            raise SessionScopeViolationError(
                requesting_session_id, resource_owner_session_id, resource_name
            )

    def violation_count(self) -> int:
        return self._violation_count
```

## Solution 4: Scoped Context Store

```python
from typing import Any, Dict, List, Optional


class ScopedContextStore:
    """
    Stores conversation context (messages, tool history) with strict
    session scoping. All reads require a matching session_id.
    """

    def __init__(
        self,
        registry: SessionNamespaceRegistry,
        enforcer: SessionScopeEnforcer,
    ):
        self._registry = registry
        self._enforcer = enforcer

    def _ns(self, session_id: str) -> SessionNamespace:
        ns = self._registry.get(session_id)
        if ns is None:
            raise ValueError(f"Unknown session '{session_id}'")
        return ns

    def append_message(self, session_id: str, role: str, content: str) -> None:
        ns = self._ns(session_id)
        messages = ns.get("messages") or []
        messages.append({"role": role, "content": content})
        ns.set("messages", messages)

    def get_messages(self, session_id: str) -> List[dict]:
        ns = self._ns(session_id)
        return list(ns.get("messages") or [])

    def set_value(self, session_id: str, key: str, value: Any) -> None:
        ns = self._ns(session_id)
        ns.set(key, value)

    def get_value(self, session_id: str, requesting_session_id: str, key: str) -> Optional[Any]:
        ns = self._ns(session_id)
        # Enforce that only the owning session can read
        self._enforcer.check(requesting_session_id, session_id, key)
        return ns.get(key)

    def clear_session(self, session_id: str) -> None:
        self._registry.destroy(session_id)
```

## Solution 5: Session Isolation Auditor

```python
import json
import time
from pathlib import Path
from threading import Lock
from typing import List


class SessionIsolationAuditor:
    """
    Records all session scope violations and data access events.
    Used to detect isolation regressions after code changes.
    """

    def __init__(self, path: str = "/tmp/session_isolation_audit.jsonl"):
        self._path = Path(path)
        self._lock = Lock()
        self._violation_count = 0

    def record_violation(self, event: dict) -> None:
        self._violation_count += 1
        record = {"ts": time.time(), "type": "scope_violation", **event}
        with self._lock:
            with self._path.open("a") as f:
                f.write(json.dumps(record) + "\n")

    def record_purge(self, session_id: str, entries_purged: int) -> None:
        record = {
            "ts": time.time(),
            "type": "session_purge",
            "session_id": session_id,
            "entries_purged": entries_purged,
        }
        with self._lock:
            with self._path.open("a") as f:
                f.write(json.dumps(record) + "\n")

    def recent_violations(self, window_seconds: float = 3600.0) -> List[dict]:
        cutoff = time.time() - window_seconds
        violations = []
        if not self._path.exists():
            return violations
        with self._lock:
            for line in self._path.read_text().splitlines():
                try:
                    e = json.loads(line)
                    if e.get("type") == "scope_violation" and e["ts"] >= cutoff:
                        violations.append(e)
                except (json.JSONDecodeError, KeyError):
                    continue
        return violations

    def violation_count(self) -> int:
        return self._violation_count
```

## Solution 6: Session Isolation Dashboard

```python
import time


class SessionIsolationDashboard:
    """
    Operational view of session isolation health: active sessions,
    violation counts, and recent purge activity.
    """

    def __init__(
        self,
        registry: SessionNamespaceRegistry,
        enforcer: SessionScopeEnforcer,
        auditor: SessionIsolationAuditor,
    ):
        self._registry = registry
        self._enforcer = enforcer
        self._auditor = auditor

    def render(self) -> dict:
        recent_violations = self._auditor.recent_violations(3600.0)
        return {
            "generated_at": time.time(),
            "active_sessions": self._registry.active_sessions(),
            "total_violations_ever": self._enforcer.violation_count(),
            "violations_last_1h": len(recent_violations),
            "recent_violations_sample": recent_violations[-5:],
            "isolation_healthy": self._enforcer.violation_count() == 0,
        }
```

## Comparison

| Approach | Namespace Scoping | Per-Session Cache | Scope Enforcement | Audit Logging | Dashboard |
|---|---|---|---|---|---|
| SessionNamespaceRegistry | Yes (TTL + eviction) | No | No | No | No |
| IsolatedToolResultCache | Via session_id key | Yes | No | No | No |
| SessionScopeEnforcer | No | No | Yes (raises) | Via callback | No |
| ScopedContextStore | Via registry | No | Via enforcer | No | No |
| SessionIsolationAuditor | No | No | No | Yes (JSONL) | No |
| SessionIsolationDashboard | No | No | No | No | Yes |

**Best for production**: Zero tolerance for scope violations — any `SessionScopeViolationError` in production is a data leakage incident, not a warning. Use `SessionNamespaceRegistry.evict_expired()` on a background scheduler (every 5 minutes) to prevent memory growth from abandoned sessions. Audit every violation immediately via `SessionIsolationAuditor` and alert on-call if the count exceeds zero in production — isolation violations should be treated as P1 security incidents. Test isolation explicitly: in your integration test suite, create two concurrent sessions, write a value in session A, and assert that session B cannot read it.
