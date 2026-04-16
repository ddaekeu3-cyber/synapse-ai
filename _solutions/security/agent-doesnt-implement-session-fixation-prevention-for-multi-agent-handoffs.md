---
title: "Agent Doesn't Implement Session Fixation Prevention for Multi-Agent Handoffs"
description: "Multi-agent systems that reuse the originating session ID when handing off to a sub-agent allow the sub-agent to operate under the parent's identity and access the parent's full session history — enabling a compromised sub-agent to exfiltrate context, impersonate the parent, or escalate privileges. Implement session fixation prevention that issues a new scoped session ID for each handoff, bounding the sub-agent to its task-specific context."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-session-fixation-prevention-for-multi-agent-handoffs
tags: [session-fixation, multi-agent, handoff-security, session-isolation, sub-agent, privilege-isolation]
symptoms:
  - "Sub-agents receive the same session ID as the orchestrator and can read the full conversation history"
  - "A compromised tool or sub-agent can access credentials or PII from earlier in the parent session"
  - "No new session boundary is created at handoff — the sub-agent operates as the parent"
  - "Sub-agent actions are attributed to the parent session in audit logs"
  - "Privilege isolation between orchestrator and sub-agent is not enforced at the session layer"
---

## Why This Happens

Multi-agent handoffs are often implemented by passing the current session context directly to the sub-agent — convenient for continuity but catastrophically broad for isolation. The sub-agent inherits the full session, including credentials, tool permissions, and prior conversation. A malicious or compromised sub-agent can extract this context. Session fixation prevention requires issuing a new session ID scoped to the handoff, passing only the context the sub-agent needs for its task, and enforcing that the sub-agent cannot access the parent session.

## Solution 1: Handoff Session Scope

```python
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set


class HandoffTrustLevel(str, Enum):
    TRUSTED = "trusted"          # same organization, same deployment
    SEMI_TRUSTED = "semi_trusted"  # known partner agent, different deployment
    UNTRUSTED = "untrusted"      # external or unknown agent


@dataclass
class HandoffSessionScope:
    parent_session_id: str
    child_session_id: str
    trust_level: HandoffTrustLevel
    allowed_tools: Set[str] = field(default_factory=set)
    allowed_context_keys: Set[str] = field(default_factory=set)
    task_description: str = ""
    created_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at

    def allows_tool(self, tool_name: str) -> bool:
        return tool_name in self.allowed_tools

    def allows_context_key(self, key: str) -> bool:
        return key in self.allowed_context_keys
```

## Solution 2: Session Fixation Prevention Manager

```python
import secrets
import time
from threading import Lock
from typing import Dict, Optional, Set


class SessionFixationPreventionManager:
    """
    Issues new scoped session IDs for every multi-agent handoff.
    Tracks parent-child relationships and enforces that child sessions
    cannot access parent session context directly.
    """

    def __init__(self, session_ttl_seconds: float = 3600.0):
        self._scopes: Dict[str, HandoffSessionScope] = {}
        self._lock = Lock()
        self._ttl = session_ttl_seconds

    def create_handoff_session(
        self,
        parent_session_id: str,
        trust_level: HandoffTrustLevel,
        allowed_tools: Set[str],
        allowed_context_keys: Set[str],
        task_description: str = "",
        ttl_seconds: Optional[float] = None,
    ) -> HandoffSessionScope:
        child_id = f"sub_{secrets.token_hex(12)}"
        scope = HandoffSessionScope(
            parent_session_id=parent_session_id,
            child_session_id=child_id,
            trust_level=trust_level,
            allowed_tools=set(allowed_tools),
            allowed_context_keys=set(allowed_context_keys),
            task_description=task_description,
            expires_at=(
                time.time() + (ttl_seconds or self._ttl)
            ),
        )
        with self._lock:
            self._scopes[child_id] = scope
        return scope

    def get_scope(self, child_session_id: str) -> Optional[HandoffSessionScope]:
        with self._lock:
            scope = self._scopes.get(child_session_id)
            if scope and scope.is_expired():
                del self._scopes[child_session_id]
                return None
            return scope

    def validate_tool_access(self, child_session_id: str, tool_name: str) -> bool:
        scope = self.get_scope(child_session_id)
        if scope is None:
            return False
        return scope.allows_tool(tool_name)

    def revoke(self, child_session_id: str) -> None:
        with self._lock:
            self._scopes.pop(child_session_id, None)

    def evict_expired(self) -> int:
        with self._lock:
            expired = [
                cid for cid, scope in self._scopes.items()
                if scope.is_expired()
            ]
            for cid in expired:
                del self._scopes[cid]
            return len(expired)

    def stats(self) -> dict:
        with self._lock:
            return {
                "active_handoff_sessions": len(self._scopes),
                "trust_breakdown": {
                    level.value: sum(
                        1 for s in self._scopes.values()
                        if s.trust_level == level
                    )
                    for level in HandoffTrustLevel
                },
            }
```

## Solution 3: Scoped Context Extractor

```python
from typing import Any, Dict


class ScopedContextExtractor:
    """
    Extracts only the context keys explicitly allowed in the handoff scope
    from the parent session context. Prevents data leakage to sub-agents.
    """

    def extract(
        self,
        parent_context: Dict[str, Any],
        scope: HandoffSessionScope,
    ) -> Dict[str, Any]:
        extracted = {}
        for key in scope.allowed_context_keys:
            if key in parent_context:
                extracted[key] = parent_context[key]
        return extracted

    def extract_for_trust_level(
        self,
        parent_context: Dict[str, Any],
        scope: HandoffSessionScope,
    ) -> Dict[str, Any]:
        """
        Applies additional restrictions based on trust level.
        UNTRUSTED agents receive no context beyond their task description.
        """
        if scope.trust_level == HandoffTrustLevel.UNTRUSTED:
            return {"task": scope.task_description}
        if scope.trust_level == HandoffTrustLevel.SEMI_TRUSTED:
            # Only explicitly whitelisted keys, no credentials
            safe = self.extract(parent_context, scope)
            return {k: v for k, v in safe.items()
                    if "key" not in k.lower()
                    and "token" not in k.lower()
                    and "secret" not in k.lower()}
        return self.extract(parent_context, scope)
```

## Solution 4: Handoff-Scoped Tool Access Enforcer

```python
from typing import Any, Callable


class HandoffScopedToolAccessEnforcer:
    """
    Enforces that a sub-agent can only call tools within its handoff scope.
    Raises HandoffToolAccessDeniedError for out-of-scope tool calls.
    """

    def __init__(self, manager: SessionFixationPreventionManager):
        self._manager = manager
        self._denied = 0

    async def enforce(
        self,
        child_session_id: str,
        tool_name: str,
        fn: Callable,
        **kwargs: Any,
    ) -> Any:
        if not self._manager.validate_tool_access(child_session_id, tool_name):
            self._denied += 1
            raise HandoffToolAccessDeniedError(child_session_id, tool_name)
        return await fn(**kwargs)

    def denied_count(self) -> int:
        return self._denied


class HandoffToolAccessDeniedError(Exception):
    def __init__(self, child_session_id: str, tool_name: str):
        super().__init__(
            f"sub-agent session '{child_session_id}' denied access to tool '{tool_name}'"
        )
        self.child_session_id = child_session_id
        self.tool_name = tool_name
```

## Solution 5: Handoff Audit Logger

```python
import time
from typing import Any, List


class HandoffAuditLogger:
    """
    Records every handoff session creation, tool access denial,
    and session revocation for security forensics.
    """

    def __init__(self, max_records: int = 10000):
        self._records: List[dict] = []
        self._max = max_records

    def record_creation(self, scope: HandoffSessionScope) -> None:
        self._append({
            "event": "handoff_created",
            "parent_session_id": scope.parent_session_id,
            "child_session_id": scope.child_session_id,
            "trust_level": scope.trust_level.value,
            "allowed_tools": list(scope.allowed_tools),
            "task": scope.task_description[:200],
        })

    def record_denial(self, child_session_id: str, tool_name: str) -> None:
        self._append({
            "event": "tool_access_denied",
            "child_session_id": child_session_id,
            "tool_name": tool_name,
        })

    def record_revocation(self, child_session_id: str) -> None:
        self._append({
            "event": "session_revoked",
            "child_session_id": child_session_id,
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
            "handoffs_created": sum(1 for r in recent if r["event"] == "handoff_created"),
            "tool_denials": sum(1 for r in recent if r["event"] == "tool_access_denied"),
            "sessions_revoked": sum(1 for r in recent if r["event"] == "session_revoked"),
        }
```

## Solution 6: Handoff Security Dashboard

```python
import time


class HandoffSecurityDashboard:
    """Combines manager stats, enforcer metrics, and audit summary."""

    def __init__(
        self,
        manager: SessionFixationPreventionManager,
        enforcer: HandoffScopedToolAccessEnforcer,
        audit_logger: HandoffAuditLogger,
    ):
        self._manager = manager
        self._enforcer = enforcer
        self._audit = audit_logger

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "session_manager": self._manager.stats(),
            "tool_access_denials": self._enforcer.denied_count(),
            "audit_summary": self._audit.summary(window_seconds=3600.0),
        }
```

## Comparison

| Approach | New Session ID | Scoped Tools | Context Filtering | Trust Levels | Audit |
|---|---|---|---|---|---|
| HandoffSessionScope | Yes (secrets) | Yes | Yes (allowed_keys) | Yes (3 levels) | No |
| SessionFixationPreventionManager | Yes | Via scope | No | Via scope | No |
| ScopedContextExtractor | No | No | Yes (trust-aware) | Yes | No |
| HandoffScopedToolAccessEnforcer | No | Yes (enforced) | No | No | No |
| HandoffAuditLogger | No | No | No | No | Yes |

**Best for production**: Always issue a new session ID for every sub-agent handoff — never pass the parent session ID. Use `HandoffTrustLevel.UNTRUSTED` for any sub-agent not deployed in the same infrastructure as the orchestrator; these agents receive only their task description and no context. Set TTL equal to the expected sub-agent task duration plus 20% margin — a sub-agent that runs past its session TTL is either stuck or abusive, and the expired session prevents further tool calls. Monitor `tool_denials` in `HandoffAuditLogger`: a sub-agent repeatedly attempting denied tools is a signal of either misconfigured scope (fix the allowed_tools set) or a compromised agent (revoke the session immediately).
