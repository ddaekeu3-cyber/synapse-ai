---
title: "Agent Doesn't Implement Tool Permission Scope Enforcement"
description: "Agents that grant all tools to all sessions allow any conversation to invoke destructive operations — a customer-facing chat agent with a database-write tool is one prompt injection away from data corruption. Implement tool permission scope enforcement that assigns tools to named permission scopes, binds sessions to scopes based on user role, and blocks tool dispatch when the session's scope does not include the requested tool."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-tool-permission-scope-enforcement
tags: [tool-permissions, permission-scopes, rbac, tool-access-control, least-privilege, authorization]
symptoms:
  - "All tools are available to all users — no role-based tool restriction"
  - "A read-only user can invoke a tool that writes to the database"
  - "No audit log of which tools were invoked by which session"
  - "Tool access is controlled at the UI layer only — the agent itself applies no enforcement"
  - "Prompt injection could invoke administrative tools from a user-level session"
---

## Why This Happens

Tool registries are typically flat — every registered tool is available to every session. Role-based access control is often applied at the UI layer (hiding buttons) but not enforced in the agent dispatch layer. A prompt injection in a retrieved document can instruct the agent to call a tool the user could never invoke manually. Defense-in-depth requires enforcement at the tool dispatch layer, not just the presentation layer: the agent must check whether the current session's permission scope includes the requested tool before invoking it.

## Solution 1: Permission Scope Definition

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet, Optional, Set


class PermissionLevel(str, Enum):
    READ = "read"
    WRITE = "write"
    ADMIN = "admin"
    SYSTEM = "system"   # internal/automated only, never user-accessible


@dataclass(frozen=True)
class ToolPermission:
    tool_name: str
    required_level: PermissionLevel
    requires_mfa: bool = False
    audit_required: bool = True
    description: str = ""


@dataclass
class PermissionScope:
    scope_id: str
    display_name: str
    allowed_tools: FrozenSet[str]
    permission_level: PermissionLevel
    max_tool_calls_per_session: int = 1000
    allowed_tool_patterns: FrozenSet[str] = field(default_factory=frozenset)

    def allows(self, tool_name: str) -> bool:
        if tool_name in self.allowed_tools:
            return True
        import fnmatch
        return any(
            fnmatch.fnmatch(tool_name, pattern)
            for pattern in self.allowed_tool_patterns
        )
```

## Solution 2: Permission Scope Registry

```python
from typing import Dict, List, Optional


class PermissionScopeRegistry:
    """
    Maintains the mapping of scope_id -> PermissionScope and
    tool_name -> ToolPermission. Provides scope resolution for sessions.
    """

    def __init__(self) -> None:
        self._scopes: Dict[str, PermissionScope] = {}
        self._tool_permissions: Dict[str, ToolPermission] = {}

    def register_scope(self, scope: PermissionScope) -> None:
        self._scopes[scope.scope_id] = scope

    def register_tool_permission(self, perm: ToolPermission) -> None:
        self._tool_permissions[perm.tool_name] = perm

    def get_scope(self, scope_id: str) -> Optional[PermissionScope]:
        return self._scopes.get(scope_id)

    def get_tool_permission(self, tool_name: str) -> Optional[ToolPermission]:
        return self._tool_permissions.get(tool_name)

    def tools_for_scope(self, scope_id: str) -> List[str]:
        scope = self._scopes.get(scope_id)
        if not scope:
            return []
        return [t for t in self._tool_permissions if scope.allows(t)]

    def all_scopes(self) -> List[PermissionScope]:
        return list(self._scopes.values())


def build_default_registry() -> PermissionScopeRegistry:
    reg = PermissionScopeRegistry()

    # Define scopes
    reg.register_scope(PermissionScope(
        scope_id="anonymous",
        display_name="Anonymous / Public",
        allowed_tools=frozenset({"web_search", "calculator", "weather"}),
        permission_level=PermissionLevel.READ,
        max_tool_calls_per_session=20,
    ))
    reg.register_scope(PermissionScope(
        scope_id="user",
        display_name="Authenticated User",
        allowed_tools=frozenset({
            "web_search", "calculator", "weather",
            "read_user_profile", "read_documents", "list_files",
        }),
        permission_level=PermissionLevel.READ,
        max_tool_calls_per_session=200,
    ))
    reg.register_scope(PermissionScope(
        scope_id="power_user",
        display_name="Power User",
        allowed_tools=frozenset({
            "web_search", "calculator", "weather",
            "read_user_profile", "read_documents", "list_files",
            "write_document", "update_profile", "send_notification",
        }),
        permission_level=PermissionLevel.WRITE,
        max_tool_calls_per_session=500,
    ))
    reg.register_scope(PermissionScope(
        scope_id="admin",
        display_name="Administrator",
        allowed_tools=frozenset(),   # empty = allow all via pattern
        allowed_tool_patterns=frozenset({"*"}),
        permission_level=PermissionLevel.ADMIN,
        max_tool_calls_per_session=5000,
    ))

    return reg
```

## Solution 3: Session Permission Context

```python
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class SessionPermissionContext:
    session_id: str
    user_id: Optional[str]
    scope_id: str
    granted_at: float = field(default_factory=time.time)
    tool_call_count: int = 0
    mfa_verified: bool = False
    context_metadata: Dict = field(default_factory=dict)

    def increment_call_count(self) -> None:
        self.tool_call_count += 1

    def age_seconds(self) -> float:
        return time.time() - self.granted_at


class SessionPermissionStore:
    """Maps active session IDs to their permission contexts."""

    def __init__(self) -> None:
        self._contexts: Dict[str, SessionPermissionContext] = {}

    def grant(self, ctx: SessionPermissionContext) -> None:
        self._contexts[ctx.session_id] = ctx

    def get(self, session_id: str) -> Optional[SessionPermissionContext]:
        return self._contexts.get(session_id)

    def revoke(self, session_id: str) -> None:
        self._contexts.pop(session_id, None)

    def active_count(self) -> int:
        return len(self._contexts)
```

## Solution 4: Permission-Enforced Tool Dispatcher

```python
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class ToolDispatchDecision:
    allowed: bool
    tool_name: str
    session_id: str
    scope_id: Optional[str]
    denial_reason: Optional[str] = None
    decided_at: float = field(default_factory=time.time)


class PermissionEnforcedToolDispatcher:
    """
    Enforces permission scope checks before every tool invocation.
    Raises PermissionError on unauthorized tool calls.
    Logs all decisions for audit trail.
    """

    def __init__(
        self,
        registry: PermissionScopeRegistry,
        session_store: SessionPermissionStore,
    ) -> None:
        self._registry = registry
        self._store = session_store
        self._audit_log: List[ToolDispatchDecision] = []

    def _decide(self, session_id: str, tool_name: str) -> ToolDispatchDecision:
        ctx = self._store.get(session_id)
        if not ctx:
            return ToolDispatchDecision(
                allowed=False,
                tool_name=tool_name,
                session_id=session_id,
                scope_id=None,
                denial_reason="no_permission_context",
            )

        scope = self._registry.get_scope(ctx.scope_id)
        if not scope:
            return ToolDispatchDecision(
                allowed=False,
                tool_name=tool_name,
                session_id=session_id,
                scope_id=ctx.scope_id,
                denial_reason="unknown_scope",
            )

        if ctx.tool_call_count >= scope.max_tool_calls_per_session:
            return ToolDispatchDecision(
                allowed=False,
                tool_name=tool_name,
                session_id=session_id,
                scope_id=ctx.scope_id,
                denial_reason="session_tool_call_limit_exceeded",
            )

        tool_perm = self._registry.get_tool_permission(tool_name)
        if tool_perm and tool_perm.requires_mfa and not ctx.mfa_verified:
            return ToolDispatchDecision(
                allowed=False,
                tool_name=tool_name,
                session_id=session_id,
                scope_id=ctx.scope_id,
                denial_reason="mfa_required",
            )

        if not scope.allows(tool_name):
            return ToolDispatchDecision(
                allowed=False,
                tool_name=tool_name,
                session_id=session_id,
                scope_id=ctx.scope_id,
                denial_reason="tool_not_in_scope",
            )

        return ToolDispatchDecision(
            allowed=True,
            tool_name=tool_name,
            session_id=session_id,
            scope_id=ctx.scope_id,
        )

    async def dispatch(
        self,
        session_id: str,
        tool_name: str,
        tool_fn: Callable,
        args: Dict[str, Any],
    ) -> Any:
        decision = self._decide(session_id, tool_name)
        self._audit_log.append(decision)

        if not decision.allowed:
            raise PermissionError(
                f"Tool '{tool_name}' denied for session '{session_id}': "
                f"{decision.denial_reason}"
            )

        ctx = self._store.get(session_id)
        if ctx:
            ctx.increment_call_count()

        return await tool_fn(**args)

    def audit_log(self, denied_only: bool = False) -> List[ToolDispatchDecision]:
        if denied_only:
            return [d for d in self._audit_log if not d.allowed]
        return list(self._audit_log)
```

## Solution 5: Permission Violation Detector

```python
import time
from collections import Counter
from typing import List


class PermissionViolationDetector:
    """
    Analyzes the dispatch audit log for suspicious patterns:
    repeated denials from a single session (possible injection attack),
    attempts to invoke SYSTEM-level tools from user sessions.
    """

    def __init__(
        self,
        dispatcher: PermissionEnforcedToolDispatcher,
        denial_storm_threshold: int = 10,
        window_seconds: float = 300.0,
    ) -> None:
        self._dispatcher = dispatcher
        self._threshold = denial_storm_threshold
        self._window = window_seconds

    def check(self) -> List[dict]:
        denied = self._dispatcher.audit_log(denied_only=True)
        now = time.time()
        recent_denied = [d for d in denied if now - d.decided_at <= self._window]

        alerts = []
        by_session = Counter(d.session_id for d in recent_denied)
        for session_id, count in by_session.items():
            if count >= self._threshold:
                alerts.append({
                    "type": "denial_storm",
                    "session_id": session_id,
                    "denial_count": count,
                    "window_seconds": self._window,
                    "severity": "critical",
                    "message": (
                        f"Session '{session_id}' had {count} denied tool calls in "
                        f"{self._window}s — possible prompt injection attack"
                    ),
                })

        scope_violations = [d for d in recent_denied if d.denial_reason == "tool_not_in_scope"]
        if len(scope_violations) >= self._threshold:
            alerts.append({
                "type": "scope_boundary_probing",
                "count": len(scope_violations),
                "severity": "warning",
                "message": "High rate of out-of-scope tool attempts detected fleet-wide",
            })

        return alerts
```

## Solution 6: Permission Scope Dashboard

```python
import time


class PermissionScopeDashboard:
    """
    Combines scope registry, session stats, dispatch audit,
    and violation detection into a security operational view.
    """

    def __init__(
        self,
        registry: PermissionScopeRegistry,
        session_store: SessionPermissionStore,
        dispatcher: PermissionEnforcedToolDispatcher,
        detector: PermissionViolationDetector,
    ) -> None:
        self._registry = registry
        self._sessions = session_store
        self._dispatcher = dispatcher
        self._detector = detector

    def render(self) -> dict:
        audit = self._dispatcher.audit_log()
        denied = [d for d in audit if not d.allowed]
        alerts = self._detector.check()

        denial_by_reason: dict = {}
        for d in denied:
            denial_by_reason[d.denial_reason] = denial_by_reason.get(d.denial_reason, 0) + 1

        return {
            "generated_at": time.time(),
            "active_sessions": self._sessions.active_count(),
            "dispatch_summary": {
                "total_dispatches": len(audit),
                "denied": len(denied),
                "allowed": len(audit) - len(denied),
                "denial_rate": round(len(denied) / max(len(audit), 1), 4),
                "denial_by_reason": denial_by_reason,
            },
            "active_alerts": alerts,
        }
```

## Comparison

| Approach | Scope Definition | Session Binding | Dispatch Enforcement | Audit Trail | Violation Detection |
|---|---|---|---|---|---|
| PermissionScopeRegistry | Yes | No | No | No | No |
| SessionPermissionStore | No | Yes | No | No | No |
| PermissionEnforcedToolDispatcher | Via registry | Via store | Yes | Yes | No |
| PermissionViolationDetector | No | No | No | Via dispatcher | Yes |
| PermissionScopeDashboard | No | No | No | Via dispatcher | Via detector |

**Best for production**: Define scopes aligned with your user roles and apply the principle of least privilege — `anonymous` sessions should have access to 3–5 read-only public tools maximum. Never grant `ADMIN` scope based on user input alone; require server-side role verification. Log all denied dispatch decisions immediately — a burst of `tool_not_in_scope` denials from a single session within seconds is the signature of a prompt injection attempting to enumerate available tools. Run `PermissionViolationDetector.check()` after every tool dispatch decision, not just periodically.
