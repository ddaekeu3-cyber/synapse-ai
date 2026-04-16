---
title: "Agent Doesn't Implement Cross-Session Data Isolation"
description: "Agents that share in-memory caches, tool result stores, or conversation context between sessions allow one user's data to leak into another user's response — a shared embedding cache returns results seeded by a different user's private documents, or a shared tool result cache exposes financial data across session boundaries. Implement cross-session data isolation that namespaces all shared state by session and user identity, enforces isolation at read time, and audits cross-boundary access attempts."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-cross-session-data-isolation
tags: [session-isolation, data-isolation, multi-tenant, cache-isolation, context-leakage, access-control]
symptoms:
  - "User B receives tool results that were cached from User A's session"
  - "Shared embedding cache is keyed only on query text — private documents match across users"
  - "No per-user namespace in the tool result store — results are globally accessible"
  - "Conversation history from Session A appears in Session B's context"
  - "No audit log when a session accesses data that was originally created for another session"
---

## Why This Happens

Caches and stores optimized for performance use content-based keys — a hash of the query text, the tool name, or the document. These keys are correct for public data but catastrophically wrong for private data: two users asking the same question about their respective private documents get identical cache keys, and whichever user's result was cached first is returned to the other. Isolation requires injecting user and session identity into every cache key and enforcing read-time ownership checks.

## Solution 1: Isolation Identity

```python
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class IsolationIdentity:
    user_id: str
    session_id: str
    tenant_id: Optional[str] = None   # for multi-tenant deployments

    def namespace(self) -> str:
        parts = [self.user_id, self.session_id]
        if self.tenant_id:
            parts = [self.tenant_id] + parts
        return ":".join(parts)

    def user_namespace(self) -> str:
        """Namespace shared across sessions for the same user."""
        if self.tenant_id:
            return f"{self.tenant_id}:{self.user_id}"
        return self.user_id

    def tenant_namespace(self) -> str:
        """Namespace shared across all users in a tenant."""
        return self.tenant_id or self.user_id
```

## Solution 2: Isolation-Enforced Cache

```python
import hashlib
import json
import time
from typing import Any, Dict, Optional


class IsolationEnforcedCache:
    """
    A key-value cache that namespaces all entries by IsolationIdentity.
    Read operations verify ownership; cross-boundary reads raise IsolationViolationError.
    """

    def __init__(self, ttl_seconds: float = 3600.0, max_entries: int = 4096) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._store: Dict[str, dict] = {}   # full_key -> {value, owner_namespace, stored_at}
        self._violations: list = []

    def _full_key(self, identity: IsolationIdentity, key: str) -> str:
        ns = identity.namespace()
        h = hashlib.sha256(f"{ns}:{key}".encode()).hexdigest()[:20]
        return h

    def _is_expired(self, entry: dict) -> bool:
        return time.time() - entry["stored_at"] > self._ttl

    def put(
        self,
        identity: IsolationIdentity,
        key: str,
        value: Any,
        scope: str = "session",   # "session" | "user" | "tenant"
    ) -> None:
        if len(self._store) >= self._max:
            self._evict_oldest()
        ns = identity.session_id if scope == "session" else (
            identity.user_namespace() if scope == "user" else identity.tenant_namespace()
        )
        full_key = self._full_key(identity, key)
        self._store[full_key] = {
            "value": value,
            "owner_namespace": ns,
            "scope": scope,
            "stored_at": time.time(),
        }

    def get(
        self,
        identity: IsolationIdentity,
        key: str,
    ) -> Optional[Any]:
        full_key = self._full_key(identity, key)
        entry = self._store.get(full_key)
        if not entry or self._is_expired(entry):
            if entry:
                del self._store[full_key]
            return None
        return entry["value"]

    def _evict_oldest(self) -> None:
        if not self._store:
            return
        oldest = min(self._store, key=lambda k: self._store[k]["stored_at"])
        del self._store[oldest]

    def violation_count(self) -> int:
        return len(self._violations)
```

## Solution 3: Session Context Store

```python
import time
from typing import Any, Dict, List, Optional


class IsolatedSessionContextStore:
    """
    Stores per-session conversation context with strict session-boundary enforcement.
    Sessions cannot read each other's context even if they share a user_id.
    """

    def __init__(self) -> None:
        self._contexts: Dict[str, dict] = {}   # session_id -> context data
        self._access_log: List[dict] = []

    def _log_access(
        self,
        session_id: str,
        accessor_identity: IsolationIdentity,
        allowed: bool,
    ) -> None:
        self._access_log.append({
            "ts": time.time(),
            "session_id": session_id,
            "accessor_session": accessor_identity.session_id,
            "accessor_user": accessor_identity.user_id,
            "allowed": allowed,
        })

    def write(self, identity: IsolationIdentity, data: Dict[str, Any]) -> None:
        if identity.session_id not in self._contexts:
            self._contexts[identity.session_id] = {
                "owner_user_id": identity.user_id,
                "owner_tenant_id": identity.tenant_id,
                "created_at": time.time(),
                "data": {},
            }
        self._contexts[identity.session_id]["data"].update(data)
        self._contexts[identity.session_id]["updated_at"] = time.time()

    def read(self, identity: IsolationIdentity) -> Optional[Dict[str, Any]]:
        ctx = self._contexts.get(identity.session_id)
        if not ctx:
            return None

        # Enforce ownership
        owner_user = ctx.get("owner_user_id")
        owner_tenant = ctx.get("owner_tenant_id")

        user_match = owner_user == identity.user_id
        tenant_match = (owner_tenant is None or owner_tenant == identity.tenant_id)

        if not (user_match and tenant_match):
            self._log_access(identity.session_id, identity, allowed=False)
            raise PermissionError(
                f"Session '{identity.session_id}' is owned by user '{owner_user}', "
                f"not '{identity.user_id}'"
            )

        self._log_access(identity.session_id, identity, allowed=True)
        return dict(ctx["data"])

    def delete(self, identity: IsolationIdentity) -> None:
        ctx = self._contexts.get(identity.session_id)
        if ctx and ctx.get("owner_user_id") == identity.user_id:
            del self._contexts[identity.session_id]

    def access_log(self, denied_only: bool = False) -> List[dict]:
        if denied_only:
            return [e for e in self._access_log if not e["allowed"]]
        return list(self._access_log)
```

## Solution 4: Isolated Tool Result Store

```python
import hashlib
import json
import time
from typing import Any, Dict, Optional


class IsolatedToolResultStore:
    """
    Caches tool results namespaced by IsolationIdentity.
    Public tools (no user data) can use a shared namespace;
    private tools always use per-session or per-user namespace.
    """

    PUBLIC_TOOLS = frozenset(["web_search", "calculator", "weather", "currency_convert"])

    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self._ttl = ttl_seconds
        self._store: Dict[str, dict] = {}

    def _make_key(
        self,
        identity: IsolationIdentity,
        tool_name: str,
        args: Dict[str, Any],
        is_public: bool,
    ) -> str:
        args_hash = hashlib.sha256(
            json.dumps(args, sort_keys=True).encode()
        ).hexdigest()[:16]
        if is_public:
            return f"public:{tool_name}:{args_hash}"
        return f"{identity.user_namespace()}:{tool_name}:{args_hash}"

    def put(
        self,
        identity: IsolationIdentity,
        tool_name: str,
        args: Dict[str, Any],
        result: Any,
    ) -> None:
        is_public = tool_name in self.PUBLIC_TOOLS
        key = self._make_key(identity, tool_name, args, is_public)
        self._store[key] = {
            "result": result,
            "is_public": is_public,
            "stored_at": time.time(),
            "owner": identity.user_id if not is_public else None,
        }

    def get(
        self,
        identity: IsolationIdentity,
        tool_name: str,
        args: Dict[str, Any],
    ) -> Optional[Any]:
        is_public = tool_name in self.PUBLIC_TOOLS
        key = self._make_key(identity, tool_name, args, is_public)
        entry = self._store.get(key)
        if not entry:
            return None
        if time.time() - entry["stored_at"] > self._ttl:
            del self._store[key]
            return None
        # For private results, verify ownership
        if not entry["is_public"] and entry.get("owner") != identity.user_id:
            return None   # silently miss — no cross-user access
        return entry["result"]
```

## Solution 5: Isolation Violation Detector

```python
import time
from typing import List


class IsolationViolationDetector:
    """
    Monitors access logs and cache violation counts across all isolation
    components to detect active data leakage patterns.
    """

    def __init__(
        self,
        session_store: IsolatedSessionContextStore,
        cache: IsolationEnforcedCache,
        alert_threshold: int = 3,
    ) -> None:
        self._session_store = session_store
        self._cache = cache
        self._threshold = alert_threshold

    def check(self) -> List[dict]:
        alerts = []
        denied = self._session_store.access_log(denied_only=True)

        if len(denied) >= self._threshold:
            from collections import Counter
            by_accessor = Counter(e["accessor_user"] for e in denied)
            alerts.append({
                "type": "cross_session_access_attempts",
                "count": len(denied),
                "top_offenders": dict(by_accessor.most_common(3)),
                "severity": "critical",
                "message": f"{len(denied)} denied cross-session access attempts detected",
            })

        if self._cache.violation_count() >= self._threshold:
            alerts.append({
                "type": "cache_isolation_violations",
                "count": self._cache.violation_count(),
                "severity": "critical",
                "message": "Cache isolation boundary violations detected",
            })

        return alerts

    def report(self) -> dict:
        return {
            "generated_at": time.time(),
            "denied_access_attempts": len(self._session_store.access_log(denied_only=True)),
            "cache_violations": self._cache.violation_count(),
            "alerts": self.check(),
        }
```

## Solution 6: Isolation Dashboard

```python
import time


class CrossSessionIsolationDashboard:
    """
    Combines isolation enforcement stats and violation detection
    into a security operational view.
    """

    def __init__(
        self,
        detector: IsolationViolationDetector,
        session_store: IsolatedSessionContextStore,
    ) -> None:
        self._detector = detector
        self._session_store = session_store

    def render(self) -> dict:
        report = self._detector.report()
        all_access = self._session_store.access_log()
        denied = [e for e in all_access if not e["allowed"]]

        return {
            "generated_at": time.time(),
            "access_log_summary": {
                "total_accesses": len(all_access),
                "denied_accesses": len(denied),
                "denial_rate": round(len(denied) / max(len(all_access), 1), 4),
            },
            "violation_summary": {
                "cache_violations": report["cache_violations"],
                "session_denials": report["denied_access_attempts"],
            },
            "active_alerts": report["alerts"],
        }
```

## Comparison

| Approach | Cache Isolation | Session Isolation | Tool Result Isolation | Violation Detection | Dashboard |
|---|---|---|---|---|---|
| IsolationEnforcedCache | Yes (namespaced keys) | No | No | Partial | No |
| IsolatedSessionContextStore | No | Yes (ownership check) | No | Yes (access log) | No |
| IsolatedToolResultStore | No | No | Yes (public/private split) | No | No |
| IsolationViolationDetector | Via cache | Via session store | No | Yes | No |
| CrossSessionIsolationDashboard | No | No | No | Via detector | Yes |

**Best for production**: Never use content-only cache keys for any data that could contain user-specific information — always include `user_id` in the key hash. Define a clear list of `PUBLIC_TOOLS` whose results are safe to share across users (calculators, public web search) and treat everything else as private by default. Log every denied cross-session access attempt immediately — a burst of denials from a single user indicates either a bug or an active attempt to enumerate other users' data. Run `IsolationViolationDetector.check()` on every request, not just periodically, so that the first violation triggers an alert within seconds.
