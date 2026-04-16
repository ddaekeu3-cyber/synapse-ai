---
title: "Agent Doesn't Implement Conversation History Access Control"
description: "Agents that store conversation history in a shared or loosely-scoped store allow one user to retrieve another user's conversation turns: a multi-tenant deployment where sessions share a Redis key prefix, an admin endpoint that returns full history without permission checks, or a context-loading bug that pulls the wrong user's history. Implement conversation history access control with per-user ownership enforcement, read/write permission scoping, and access attempt audit logging."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-conversation-history-access-control
tags: [history-access-control, multi-tenant, conversation-privacy, authorization, data-isolation, session-ownership]
symptoms:
  - "History retrieval API returns turns from any session ID without ownership validation"
  - "Multi-tenant deployment stores all histories under a shared prefix without user scoping"
  - "An admin tool can retrieve any user's full conversation without permission checks"
  - "Context loading uses the session ID from the request without verifying the requesting user owns it"
  - "No audit log of which user accessed which conversation history"
---

## Why This Happens

Conversation history stores are typically keyed by session ID alone, with no binding between the session and the user who owns it. A bug or intentional exploit that supplies an arbitrary session ID returns that session's history. Even when history endpoints are not publicly exposed, internal tooling and admin APIs often bypass authorization checks. Access control requires storing an ownership record (user_id → session_id mapping) at session creation, validating ownership on every history read and write, and logging every access attempt so unauthorized reads are detectable after the fact.

## Solution 1: Conversation Ownership Record

```python
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional, Set


@dataclass
class ConversationOwnershipRecord:
    session_id: str
    owner_user_id: str
    created_at: float = field(default_factory=time.time)
    permitted_reader_ids: Set[str] = field(default_factory=set)
    # users who can read (not write) this conversation
    is_shared: bool = False
    deleted_at: Optional[float] = None

    def is_readable_by(self, user_id: str) -> bool:
        if self.deleted_at:
            return False
        return user_id == self.owner_user_id or user_id in self.permitted_reader_ids

    def is_writable_by(self, user_id: str) -> bool:
        if self.deleted_at:
            return False
        return user_id == self.owner_user_id
```

## Solution 2: Ownership Registry

```python
import threading
import time
from typing import Dict, List, Optional, Set


class ConversationOwnershipRegistry:
    """
    Stores and validates ownership records for conversation sessions.
    Thread-safe for concurrent access in multi-session environments.
    """

    def __init__(self):
        self._records: Dict[str, ConversationOwnershipRecord] = {}
        self._user_sessions: Dict[str, Set[str]] = {}   # user_id -> session_ids
        self._lock = threading.Lock()

    def register(
        self,
        session_id: str,
        owner_user_id: str,
    ) -> ConversationOwnershipRecord:
        with self._lock:
            record = ConversationOwnershipRecord(
                session_id=session_id,
                owner_user_id=owner_user_id,
            )
            self._records[session_id] = record
            self._user_sessions.setdefault(owner_user_id, set()).add(session_id)
            return record

    def get(self, session_id: str) -> Optional[ConversationOwnershipRecord]:
        with self._lock:
            return self._records.get(session_id)

    def check_read(self, session_id: str, requesting_user_id: str) -> bool:
        record = self.get(session_id)
        if record is None:
            return False
        return record.is_readable_by(requesting_user_id)

    def check_write(self, session_id: str, requesting_user_id: str) -> bool:
        record = self.get(session_id)
        if record is None:
            return False
        return record.is_writable_by(requesting_user_id)

    def grant_read(self, session_id: str, user_id: str, granting_user_id: str) -> bool:
        with self._lock:
            record = self._records.get(session_id)
            if record is None or record.owner_user_id != granting_user_id:
                return False
            record.permitted_reader_ids.add(user_id)
            return True

    def user_sessions(self, user_id: str) -> List[str]:
        with self._lock:
            return list(self._user_sessions.get(user_id, set()))

    def soft_delete(self, session_id: str, requesting_user_id: str) -> bool:
        with self._lock:
            record = self._records.get(session_id)
            if record is None or record.owner_user_id != requesting_user_id:
                return False
            record.deleted_at = time.time()
            return True
```

## Solution 3: Access Control Enforcer

```python
import time
from typing import Any, Dict, List, Optional


class ConversationHistoryAccessError(Exception):
    def __init__(self, session_id: str, user_id: str, operation: str):
        super().__init__(
            f"User '{user_id}' is not authorized to {operation} "
            f"conversation history for session '{session_id}'"
        )
        self.session_id = session_id
        self.user_id = user_id
        self.operation = operation


class ConversationAccessControlEnforcer:
    """
    Enforces read and write access control on conversation history operations.
    Raises ConversationHistoryAccessError on unauthorized attempts.
    """

    def __init__(self, registry: ConversationOwnershipRegistry):
        self._registry = registry
        self._violation_log: List[dict] = []
        self._allowed_ops = 0
        self._denied_ops = 0

    def assert_read(self, session_id: str, user_id: str) -> None:
        if not self._registry.check_read(session_id, user_id):
            self._record_violation(session_id, user_id, "read")
            self._denied_ops += 1
            raise ConversationHistoryAccessError(session_id, user_id, "read")
        self._allowed_ops += 1

    def assert_write(self, session_id: str, user_id: str) -> None:
        if not self._registry.check_write(session_id, user_id):
            self._record_violation(session_id, user_id, "write")
            self._denied_ops += 1
            raise ConversationHistoryAccessError(session_id, user_id, "write")
        self._allowed_ops += 1

    def _record_violation(self, session_id: str, user_id: str, operation: str) -> None:
        self._violation_log.append({
            "session_id": session_id,
            "user_id": user_id,
            "operation": operation,
            "attempted_at": time.time(),
        })

    def recent_violations(self, limit: int = 50) -> List[dict]:
        return self._violation_log[-limit:]

    def stats(self) -> dict:
        total = self._allowed_ops + self._denied_ops
        return {
            "total_checks": total,
            "allowed": self._allowed_ops,
            "denied": self._denied_ops,
            "denial_rate": round(self._denied_ops / max(total, 1), 4),
        }
```

## Solution 4: Access-Controlled History Store

```python
import time
from typing import Any, Dict, List, Optional


class AccessControlledHistoryStore:
    """
    Wraps a history storage backend with ownership enforcement.
    Every read and write validates the requesting user's authorization.
    """

    def __init__(
        self,
        enforcer: ConversationAccessControlEnforcer,
        backend: Optional[Dict[str, List[dict]]] = None,
    ):
        self._enforcer = enforcer
        self._store: Dict[str, List[dict]] = backend if backend is not None else {}

    def append_turn(
        self,
        session_id: str,
        requesting_user_id: str,
        turn: dict,
    ) -> None:
        self._enforcer.assert_write(session_id, requesting_user_id)
        if session_id not in self._store:
            self._store[session_id] = []
        self._store[session_id].append({**turn, "recorded_at": time.time()})

    def get_history(
        self,
        session_id: str,
        requesting_user_id: str,
        limit: Optional[int] = None,
    ) -> List[dict]:
        self._enforcer.assert_read(session_id, requesting_user_id)
        history = self._store.get(session_id, [])
        if limit:
            return history[-limit:]
        return list(history)

    def delete_history(
        self,
        session_id: str,
        requesting_user_id: str,
    ) -> None:
        self._enforcer.assert_write(session_id, requesting_user_id)
        self._store.pop(session_id, None)
```

## Solution 5: History Access Audit Logger

```python
import time
from typing import List, Optional


class ConversationHistoryAuditLogger:
    """
    Records every read and write access to conversation history,
    including the requesting user and outcome, for compliance auditing.
    """

    def __init__(self, max_records: int = 100000):
        self._max = max_records
        self._log: List[dict] = []

    def log_access(
        self,
        session_id: str,
        user_id: str,
        operation: str,
        outcome: str,
        turn_count: Optional[int] = None,
    ) -> None:
        if len(self._log) >= self._max:
            self._log.pop(0)
        self._log.append({
            "ts": time.time(),
            "session_id": session_id,
            "user_id": user_id,
            "operation": operation,
            "outcome": outcome,
            "turn_count": turn_count,
        })

    def access_report(
        self,
        user_id: Optional[str] = None,
        window_seconds: float = 86400.0,
    ) -> dict:
        cutoff = time.time() - window_seconds
        recent = [
            e for e in self._log
            if e["ts"] >= cutoff
            and (user_id is None or e["user_id"] == user_id)
        ]
        denied = [e for e in recent if e["outcome"] == "denied"]
        return {
            "window_seconds": window_seconds,
            "total_accesses": len(recent),
            "denied_accesses": len(denied),
            "denial_rate": round(len(denied) / max(len(recent), 1), 4),
        }
```

## Solution 6: History Access Control Dashboard

```python
import time


class HistoryAccessControlDashboard:
    """
    Combines enforcer stats, violation log, and audit report
    into a single security posture view.
    """

    def __init__(
        self,
        enforcer: ConversationAccessControlEnforcer,
        audit_logger: ConversationHistoryAuditLogger,
    ):
        self._enforcer = enforcer
        self._audit = audit_logger

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "enforcer_stats": self._enforcer.stats(),
            "recent_violations": self._enforcer.recent_violations(limit=10),
            "audit_report": self._audit.access_report(window_seconds=3600.0),
        }
```

## Comparison

| Approach | Ownership Validation | Read/Write Enforcement | Audit Logging | Share/Grant | Dashboard |
|---|---|---|---|---|---|
| ConversationOwnershipRegistry | Yes (owner+readers) | No | No | Yes | No |
| ConversationAccessControlEnforcer | Via registry | Yes | Via violations | No | No |
| AccessControlledHistoryStore | Via enforcer | Yes (both ops) | No | No | No |
| ConversationHistoryAuditLogger | No | No | Yes | No | No |
| HistoryAccessControlDashboard | No | No | Via logger | No | Yes |

**Best for production**: Register ownership at session creation — never allow a session to exist without an owner record, as that creates an orphaned session that any user could access. Validate `requesting_user_id` from the authenticated identity in the request context, not from any user-supplied parameter — user-supplied session IDs are acceptable inputs, but the user's identity for authorization must come from the server-side auth token. Alert on any `ConversationHistoryAccessError` with `operation=read` — attempted cross-user history reads are a serious security signal that may indicate session ID enumeration or a client-side bug that is leaking session IDs across users.
