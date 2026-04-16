---
title: "Agent Doesn't Implement Tool Call Authorization Checks"
description: "Agents that allow any LLM-generated tool call to execute without checking whether the current session is authorized to invoke that tool enable privilege escalation: a user with read-only access triggers a write tool, an unauthenticated session calls an admin API, or a prompt-injection attack invokes a destructive tool. Implement tool call authorization checks that validate the session's permission set against a per-tool required-permission list before every dispatch."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-tool-call-authorization-checks
tags: [authorization, tool-permissions, rbac, privilege-escalation, access-control, tool-security]
symptoms:
  - "A read-only user session successfully invokes a write tool because no permission check exists"
  - "LLM prompt injection triggers a delete_record tool that should require admin permission"
  - "Tool authorization is checked at the UI layer but not at the agent dispatch layer"
  - "All tools are equally accessible regardless of session role or authentication state"
  - "No audit log of which session invoked which tool and whether it was authorized"
---

## Why This Happens

Tool schemas define what arguments a tool accepts, not who is allowed to call it. When the LLM generates a tool call, agents typically validate arguments against the schema and then execute immediately. There is no second gate that asks "does this session have the right to invoke this tool?". Authorization belongs at the dispatch layer — the point just before tool execution — so it cannot be bypassed by schema-valid arguments or by prompt-injection attacks that circumvent UI-level checks.

## Solution 1: Permission Model

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet, Set


class Permission(str, Enum):
    # Data access
    READ_DATA = "read:data"
    WRITE_DATA = "write:data"
    DELETE_DATA = "delete:data"
    # External calls
    CALL_EXTERNAL_API = "call:external_api"
    SEND_MESSAGE = "send:message"
    # Admin
    ADMIN_READ = "admin:read"
    ADMIN_WRITE = "admin:write"
    # Tool-specific
    USE_CODE_EXEC = "use:code_exec"
    USE_FILE_READ = "use:file_read"
    USE_FILE_WRITE = "use:file_write"
    USE_SEARCH = "use:search"


@dataclass(frozen=True)
class SessionPrincipal:
    session_id: str
    user_id: str
    role: str
    granted_permissions: FrozenSet[Permission]

    def has(self, permission: Permission) -> bool:
        return permission in self.granted_permissions

    def has_all(self, permissions: FrozenSet[Permission]) -> bool:
        return permissions.issubset(self.granted_permissions)
```

## Solution 2: Tool Permission Registry

```python
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Set


@dataclass
class ToolPermissionSpec:
    tool_name: str
    required_permissions: FrozenSet[Permission]
    deny_roles: FrozenSet[str] = field(default_factory=frozenset)
    allow_roles: Optional[FrozenSet[str]] = None   # None = all roles (subject to permissions)
    requires_authenticated: bool = True
    audit_on_call: bool = True


class ToolPermissionRegistry:
    """
    Stores required permissions for each registered tool.
    Tools not in the registry default to requiring all permissions (deny-by-default).
    """

    def __init__(self, default_deny: bool = True):
        self._specs: Dict[str, ToolPermissionSpec] = {}
        self._default_deny = default_deny

    def register(self, spec: ToolPermissionSpec) -> None:
        self._specs[spec.tool_name] = spec

    def get(self, tool_name: str) -> Optional[ToolPermissionSpec]:
        return self._specs.get(tool_name)

    def is_registered(self, tool_name: str) -> bool:
        return tool_name in self._specs

    def all_specs(self) -> List[ToolPermissionSpec]:
        return list(self._specs.values())
```

## Solution 3: Authorization Checker

```python
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class AuthorizationDecision:
    allowed: bool
    tool_name: str
    principal: SessionPrincipal
    reason: str
    missing_permissions: List[Permission]


class ToolCallAuthorizationChecker:
    """
    Evaluates whether a SessionPrincipal may invoke a named tool.
    Checks: registration, authentication, role deny/allow lists, and permissions.
    Returns an AuthorizationDecision with the reason for allow or deny.
    """

    def __init__(self, registry: ToolPermissionRegistry):
        self._registry = registry

    def check(
        self,
        tool_name: str,
        principal: SessionPrincipal,
    ) -> AuthorizationDecision:
        spec = self._registry.get(tool_name)

        if spec is None:
            if self._registry._default_deny:
                return AuthorizationDecision(
                    allowed=False,
                    tool_name=tool_name,
                    principal=principal,
                    reason="tool not registered — default deny",
                    missing_permissions=[],
                )
            return AuthorizationDecision(
                allowed=True,
                tool_name=tool_name,
                principal=principal,
                reason="tool not registered — default allow",
                missing_permissions=[],
            )

        if spec.requires_authenticated and not principal.user_id:
            return AuthorizationDecision(
                allowed=False,
                tool_name=tool_name,
                principal=principal,
                reason="unauthenticated session",
                missing_permissions=[],
            )

        if principal.role in spec.deny_roles:
            return AuthorizationDecision(
                allowed=False,
                tool_name=tool_name,
                principal=principal,
                reason=f"role '{principal.role}' is explicitly denied",
                missing_permissions=[],
            )

        if spec.allow_roles is not None and principal.role not in spec.allow_roles:
            return AuthorizationDecision(
                allowed=False,
                tool_name=tool_name,
                principal=principal,
                reason=f"role '{principal.role}' not in allow-list",
                missing_permissions=[],
            )

        missing = [
            p for p in spec.required_permissions
            if not principal.has(p)
        ]
        if missing:
            return AuthorizationDecision(
                allowed=False,
                tool_name=tool_name,
                principal=principal,
                reason=f"missing permissions: {[p.value for p in missing]}",
                missing_permissions=missing,
            )

        return AuthorizationDecision(
            allowed=True,
            tool_name=tool_name,
            principal=principal,
            reason="all checks passed",
            missing_permissions=[],
        )
```

## Solution 4: Authorization-Gated Tool Dispatcher

```python
import asyncio
from typing import Any, Callable, Dict, Optional


class AuthorizationGatedToolDispatcher:
    """
    Enforces authorization before every tool invocation.
    Raises PermissionError with the denial reason if the check fails.
    Logs every tool call attempt (allowed or denied) for audit.
    """

    def __init__(
        self,
        checker: ToolCallAuthorizationChecker,
        audit_log: Optional["ToolCallAuditLog"] = None,
    ):
        self._checker = checker
        self._audit = audit_log

    async def dispatch(
        self,
        tool_name: str,
        principal: SessionPrincipal,
        tool_fn: Callable,
        args: Dict[str, Any],
    ) -> Any:
        decision = self._checker.check(tool_name, principal)

        if self._audit:
            self._audit.record(decision, args)

        if not decision.allowed:
            raise PermissionError(
                f"Tool '{tool_name}' denied for session "
                f"'{principal.session_id}' (role='{principal.role}'): "
                f"{decision.reason}"
            )

        return await tool_fn(**args)
```

## Solution 5: Tool Call Audit Log

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolCallAuditEntry:
    tool_name: str
    session_id: str
    user_id: str
    role: str
    allowed: bool
    reason: str
    args_keys: List[str]   # log arg names only, not values (avoid secret leakage)
    timestamp: float = field(default_factory=time.time)


class ToolCallAuditLog:
    """
    Records every tool call authorization decision.
    Denied calls are tracked separately for security analysis.
    """

    def __init__(self, max_entries: int = 10_000):
        self._entries: List[ToolCallAuditEntry] = []
        self._max = max_entries

    def record(
        self,
        decision: AuthorizationDecision,
        args: Dict[str, Any],
    ) -> None:
        if len(self._entries) >= self._max:
            self._entries.pop(0)
        self._entries.append(ToolCallAuditEntry(
            tool_name=decision.tool_name,
            session_id=decision.principal.session_id,
            user_id=decision.principal.user_id,
            role=decision.principal.role,
            allowed=decision.allowed,
            reason=decision.reason,
            args_keys=list(args.keys()),
        ))

    def denied_calls(self, window_seconds: float = 3600.0) -> List[ToolCallAuditEntry]:
        cutoff = time.time() - window_seconds
        return [
            e for e in self._entries
            if not e.allowed and e.timestamp >= cutoff
        ]

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [e for e in self._entries if e.timestamp >= cutoff]
        denied = [e for e in recent if not e.allowed]
        by_tool: Dict[str, int] = {}
        for e in denied:
            by_tool[e.tool_name] = by_tool.get(e.tool_name, 0) + 1
        return {
            "total_calls": len(recent),
            "denied_calls": len(denied),
            "deny_rate": round(len(denied) / max(len(recent), 1), 4),
            "most_denied_tools": dict(sorted(by_tool.items(), key=lambda x: -x[1])[:5]),
        }
```

## Solution 6: Role-Based Permission Builder

```python
from typing import Dict, FrozenSet, Set


class RolePermissionBuilder:
    """
    Defines standard role-to-permission mappings and builds
    SessionPrincipal objects with the correct permission set for a role.
    """

    ROLE_PERMISSIONS: Dict[str, FrozenSet[Permission]] = {
        "guest": frozenset({Permission.USE_SEARCH, Permission.READ_DATA}),
        "user": frozenset({
            Permission.READ_DATA,
            Permission.USE_SEARCH,
            Permission.USE_FILE_READ,
            Permission.CALL_EXTERNAL_API,
        }),
        "power_user": frozenset({
            Permission.READ_DATA,
            Permission.WRITE_DATA,
            Permission.USE_SEARCH,
            Permission.USE_FILE_READ,
            Permission.USE_FILE_WRITE,
            Permission.CALL_EXTERNAL_API,
            Permission.SEND_MESSAGE,
            Permission.USE_CODE_EXEC,
        }),
        "admin": frozenset(Permission),   # all permissions
    }

    @classmethod
    def build(
        cls,
        session_id: str,
        user_id: str,
        role: str,
        extra_permissions: FrozenSet[Permission] = frozenset(),
    ) -> SessionPrincipal:
        base = cls.ROLE_PERMISSIONS.get(role, frozenset())
        return SessionPrincipal(
            session_id=session_id,
            user_id=user_id,
            role=role,
            granted_permissions=base | extra_permissions,
        )
```

## Comparison

| Approach | Permission Model | Role Checks | Audit Log | Default Deny | Builder |
|---|---|---|---|---|---|
| ToolPermissionRegistry | Yes (per-tool spec) | Via spec | No | Yes (configurable) | No |
| ToolCallAuthorizationChecker | Via registry | Yes (deny/allow lists) | No | Via registry | No |
| AuthorizationGatedToolDispatcher | Via checker | Via checker | Optional | Via checker | No |
| ToolCallAuditLog | No | No | Yes | No | No |
| RolePermissionBuilder | No | No | No | No | Yes |

**Best for production**: Register every tool with an explicit `ToolPermissionSpec` before deployment — default-deny means unregistered tools are automatically blocked. Use `deny_roles=frozenset({"guest"})` on any tool that modifies state. Wire `ToolCallAuditLog` into `AuthorizationGatedToolDispatcher` and push denied events to your SIEM: a session generating repeated denied calls may be under active prompt-injection attack. Review `audit_log.summary().deny_rate` daily — a sudden spike in denials after a model change indicates the model is attempting tool calls it should not.
